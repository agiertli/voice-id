# Architecture

Background for operators and presenters. Deployment specifics live in
[INSTALLATION.md](INSTALLATION.md), the Helm charts and `infra/` manifests; if this
page and those disagree, the manifests win.

## Overview

```mermaid
flowchart LR
    subgraph Booth["Booth"]
        Player["Player browser<br/>game /"]
        TV["Monitor<br/>/tv"]
        Staff["Staff<br/>/admin"]
        Rec["Speaker browser<br/>Clone Studio"]
    end

    subgraph NSGame["namespace voice-id"]
        Game["voice-id<br/>Flask + gunicorn<br/>CPU"]
        DB[("SQLite on PVC<br/>clips, players,<br/>games, leaderboard")]
        Qwen["qwen2-audio<br/>LLMInferenceService<br/>L4 GPU #1"]
        Gemma["gemma<br/>LLMInferenceService<br/>L4 GPU #2"]
        GaryB["deepfake-detector-base<br/>CPU"]
        GaryF["deepfake-detector-fine-tuned<br/>CPU"]
        XTTS["xtts-v2<br/>L4 GPU #3"]
        Fish["fish-native<br/>L4 GPU #4"]
    end

    subgraph NSStudio["namespace voice-clone-studio"]
        Studio["voice-clone-studio<br/>Flask, CPU"]
        SDB[("SQLite on PVC<br/>voices, clips,<br/>recordings")]
    end

    Player -->|HTTPS route| Game
    TV -->|"/api/leaderboard every 10 s"| Game
    Staff -->|"Bearer token"| Game
    Game --- DB
    Game -->|"raw WAV, chat/completions"| Qwen
    Game -->|"spectrogram PNG, chat/completions"| Gemma
    Game -->|"WAV, /predict"| GaryB
    Game -->|"WAV, /predict"| GaryF
    Rec -->|HTTPS route| Studio
    Studio --- SDB
    Studio -->|"clone_speaker, tts"| XTTS
    Studio -->|"references/add, v1/tts"| Fish
    Studio -.->|"import tool / seed pack<br/>(operator, via APIs)"| Game
```

The game never calls the TTS engines. Clips are made beforehand (Clone Studio or
offline scripts) and stored in the game database. XTTS and Fish serve Clone Studio
only.

## Install profiles

| Profile | Workloads | GPU nodes |
|---|---|---:|
| minimal | `voice-id`, `deepfake-detector-base`, `deepfake-detector-fine-tuned` (all CPU) | 0 |
| standard | minimal + `qwen2-audio`, `gemma` | 2 |
| full | standard + `xtts-v2`, `fish-native`, `voice-clone-studio` | 4 |

The game's runtime path never touches XTTS or Fish, so `minimal` and `standard` are
complete booths; `full` adds the clip-making side. Opponents not deployed are
disabled with Helm flags and disappear from the API, the game and `/tv`. See
[install profiles](INSTALLATION.md#install-profiles).

## Components

| Component | Image / model | Hardware (requests / limits) | Storage | Exposure |
|---|---|---|---|---|
| `voice-id` | `quay.io/agiertli/voice-id` (UBI9 Python 3.12, gunicorn 4 workers × 4 threads) | 0.5 / 2 CPU, 512 Mi / 1 Gi | PVC `voice-id-data` 1 Gi RWO (SQLite, WAL) | Route, edge TLS, 120 s timeout |
| `qwen2-audio` | vLLM via KServe LLMInferenceService, `Qwen/Qwen2-Audio-7B-Instruct`, max len 4096 | 1 L4, 1 / 4 CPU, 8 / 24 Gi | PVC `qwen2-audio-model` 20 Gi | Internal `https://qwen2-audio-kserve-workload-svc:8000/v1` |
| `gemma` | vLLM via LLMInferenceService, `RedHatAI/gemma-4-12B-it-FP8-dynamic`, served as `gemma-4-12b`, max len 8192, 1 image per prompt | 1 L4, 1 / 4 CPU, 8 / 24 Gi | PVC `gemma-model` 30 Gi | Internal `https://gemma-kserve-workload-svc:8000/v1` |
| `deepfake-detector-base` | `quay.io/agiertli/deepfake-detector-base:v1.0.3`, Wav2Vec2-XLSR pre-trained | 2 / 4 CPU, 1 / 2 Gi | none | Service :8080 (+ route) |
| `deepfake-detector-fine-tuned` | `quay.io/agiertli/deepfake-detector:v2.0.0`, V4-A fine-tune | 2 / 4 CPU, 1 / 2 Gi | none | Service :8080 (+ route) |
| `xtts-v2` | `ghcr.io/coqui-ai/xtts-streaming-server` (pinned by digest) | 1 L4, 4 CPU, 8 Gi | none | Service :80, route |
| `fish-native` | `docker.io/fishaudio/fish-speech` (pinned by digest), S2 Pro, `--compile` | 1 L4, 4 CPU, 16 Gi | PVC `fish-data` 20 Gi (weights); references in memory | Service :8080, route (300 s) |
| `voice-clone-studio` | `quay.io/agiertli/voice-clone-studio:v2.2.1` | 0.1 / 0.5 CPU, 128 / 256 Mi | PVC `voice-clone-studio-data` 1 Gi | Route, 300 s timeout |

GPU nodes (`full`; `standard` has the first two): four `g6.2xlarge` (1× NVIDIA L4 24 GB, 8 vCPU, 32 GiB), tainted
`nvidia.com/gpu=true:NoSchedule`, one GPU workload each. Every GPU workload
requests a whole GPU; there is no sharing.

## Game flow

```mermaid
sequenceDiagram
    participant B as Browser
    participant G as voice-id
    participant O as Opponent backend
    B->>G: POST /api/game/start {player_name, email}
    G-->>B: game_id + 5 random clip ids (at least 1 AI, 1 human)
    B->>G: GET /api/clip/<id>/audio (all 5, preloaded)
    opt Battle mode ("Preparing the game")
        B->>G: POST /api/game/<id>/<opponent endpoint> (5 in parallel)
        alt answer cached for this clip audio (sha256)
            G->>G: opponent_cache hit
        else cache miss
            G->>O: spectrogram / WAV / multipart
            O-->>G: guess + reasoning (stored in opponent_cache)
        end
        G-->>B: ready (answer sealed, recorded on the round)
    end
    loop each clip
        B->>G: POST /api/game/<id>/play {clip_id} (starts the clip clock)
        B->>G: POST /api/game/<id>/guess {clip_id, guess}
        G-->>B: correct?, points, truth, model, speaker, photo, opponent answer
    end
    B->>G: POST /api/game/<id>/complete
    G-->>B: score, points breakdown, game time, prize, rank
```

The server is the authority for correct answers, points, game time (first play
press to last answer) and the prize; the browser only displays them. Full rules:
[scoring](customization/scoring.md).

A background thread (one warming process at a time, chosen through a database lease)
pre-computes every opponent's answer for every clip, so the battle pre-analysis is
near-instant once warm. Details: [answer cache](customization/ai-opponents.md#answer-cache-and-background-pre-warm).

## Opponent analysis paths

| Opponent | What the server sends | Model input |
|---|---|---|
| Teddy (`qwen`) | Clip converted to 22.05 kHz mono WAV, base64 `audio_url` + forensic prompt | Audio |
| Iris (`gemma`) | Server renders a spectrogram (matplotlib, 0–8 kHz, magma), base64 `image_url` + prompt | **Image** (only opponent using spectrograms) |
| Gary (`gary-base`) | WAV as multipart to `/predict` | Waveform; verdict from `p_fake` thresholds 0.3 / 0.7 |
| Gary 2.0 (`gary-finetuned`) | Same | Same |

Details: [AI opponents](customization/ai-opponents.md).

Optional detector backend: Helm `detectorBackend: openvino` points Gary / Gary 2.0 at serve.py-compatible adapters in front of
OpenShift AI's built-in OpenVINO Model Server (namespace `voice-id-ovms`, `infra/openvino-detector/`). It is parity-tested against the legacy
Deployments, and `legacy` stays the default. Payload contract: [OPENVINO-DETECTOR.md](OPENVINO-DETECTOR.md). Switch decision: [OPENVINO-SWITCH-BRIEF.md](OPENVINO-SWITCH-BRIEF.md).

## Clip pipeline

```mermaid
flowchart LR
    Speaker["Speaker reads<br/>15 sentences (cs/sk/en)"] --> Rec["Clone Studio<br/>recordings"]
    Ref["~30 s reference"] --> Voice["Clone Studio voice<br/>registered at XTTS + Fish"]
    Voice --> Gen["Generate same sentences<br/>XTTS / Fish"]
    Rec --> Imp["import_from_clone_studio.py<br/>(dry run, then --apply)"]
    Gen --> Imp
    Imp --> GameDB[("Game clips")]
    Imp -.-> Export["~/voice-id-audio/<br/>manifest.json (training)"]
    GameDB --> Seed["Seed pack zip<br/>(private distribution)"]
    Export -.-> Train["Detector retraining<br/>experiment_v4.py"]
```

Audio exists only in the two cluster databases and in local folders outside the
repository; nothing in this pipeline touches Git.

## Data model (game)

| Table | Content |
|---|---|
| `clips` | id, filename, is_ai, language, source (TTS name), speaker, audio BLOB, content type, audio_sha256 |
| `players` | nickname (unique), email |
| `games` | id, player, clip ids, guesses JSON, score, total, points, duration, opponent, completed |
| `leaderboard` | one row per completed game: player, score, total, points, duration, opponent |
| `clip_stats` | times played, guessed AI, guessed human (per clip) |
| `speakers` | display name and photo per speaker key |
| `game_rounds` | per game and clip: first play, guess, points, the opponent's recorded answer |
| `opponent_cache` | per opponent and clip: cached answer, detector details, Iris spectrogram, audio sha256 it is valid for |
| `opponent_cache_meta`, `opponent_cache_runs` | pre-warm lease, last run, per-opponent run status |

## Design constraints

- Single replica, SQLite on a RWO volume: no HA. A pod restart interrupts games in
  progress; completed results survive on the PVC.
- No authentication for players; all game admin endpoints (including clip
  management and score reset) use one shared Bearer token; the Clone Studio API is
  unauthenticated. Game scoring is server-side.
- LLM endpoints are cluster-internal and called with TLS verification off; external
  exposure needs Red Hat Connectivity Link.
