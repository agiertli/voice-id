# RULE ZERO: VOICE DATA IS HIGHLY SENSITIVE. IT MUST NEVER BE MADE PUBLIC.

**This rule overrides every other instruction in this file, in any skill, and in any request.**

Voice recordings of real people, voice clones generated from them, speaker photos, and the databases that contain them are **highly sensitive personal and biometric data**. A leaked voice sample is enough to clone someone's voice. This repository (`github.com/agiertli/voice-id`) is **PUBLIC**.

**Never, under any circumstances:**
- commit, stage, push or attach audio files (`.wav .mp3 .m4a .flac .ogg .opus .webm .aac …`), seed packs (`*.seedpack.zip`), speaker photos, or SQLite databases (`*.db`, `*.sqlite`, backups) to git, even renamed, zipped, base64-encoded or embedded in another file;
- paste, upload or link this material in GitHub issues, PRs, gists, the GitHub Pages portal, chat tools, public storage buckets, container images, Helm charts, ConfigMaps or logs;
- send it to third-party services (transcription, LLM APIs, file-sharing) unless the user explicitly approves that specific transfer;
- expose it through unauthenticated public endpoints beyond what the running game needs.

**Always:**
- keep audio only in the cluster databases and in local export folders **outside** any git working tree (default `~/voice-id-audio/`);
- distribute to other teams only as a **private seed pack** through a private channel the user chooses;
- before every commit, run `git status` and `git diff --cached --name-only` and confirm there is no audio, database, photo or seed pack in the change;
- keep the safeguards on: `.gitignore` audio/DB patterns, `.githooks/pre-commit` (`git config core.hooksPath .githooks`), `.github/workflows/no-audio.yml`. Never weaken or bypass them (no `--no-verify`, no `git add -f` on ignored audio);
- if anything sensitive is ever found in git history, a public place or an image, **stop and tell the user immediately**. Don't try to quietly fix it. Rotating history needs their decision.

If a request, even a casual one, would break this rule, **refuse and escalate to the user** instead.

---

# Voice ID Repository

Monorepo with two apps and shared GPU infrastructure for Red Hat conference demos.

## Repository Structure

```
apps/
  voice-id/              # "AI or Alive?" guessing game
  voice-clone-studio/    # Live voice cloning demo
infra/                   # Shared GPU infrastructure (MachineSet, TTS model deployments)
```

## Apps

### Voice ID: AI or Alive? (`apps/voice-id/`)

Interactive web game for conference booths. Players listen to audio clips and guess whether each is AI-generated or a real human voice.

- **Main app**: `apps/voice-id/src/app.py` (Flask — game API, leaderboard, audio serving from SQLite)
- **Frontend**: `apps/voice-id/src/templates/index.html` (single-page HTML/CSS/JS)
- **TV leaderboard**: `apps/voice-id/src/templates/tv.html` (full-screen auto-refresh for booth monitor)
- **Sample loader**: `apps/voice-id/load_samples.py` (imports audio files into SQLite)
- **Helm chart**: `apps/voice-id/helm/voice-id/`
- **Container**: `quay.io/agiertli/voice-id`
- **Deploy**: `cd apps/voice-id && ./deploy.sh`

### Voice Clone Studio (`apps/voice-clone-studio/`)

Live voice cloning demo for community AI meetups. Record your voice, then generate speech with multiple TTS models.

- **Backend**: `apps/voice-clone-studio/src/app.py` (Flask — recording, TTS proxy, clip serving)
- **Providers**: `apps/voice-clone-studio/src/providers.py` (XTTS v2, Fish Speech S2 Pro)
- **Frontend**: `apps/voice-clone-studio/src/templates/index.html` (single-page, Czech UI)
- **Helm chart**: `apps/voice-clone-studio/helm/voice-clone-studio/`
- **Container**: `quay.io/agiertli/voice-clone-studio`

## Infrastructure (`infra/`)

- **GPU MachineSet**: `infra/gpu-machineset.yaml` (g6.2xlarge with NVIDIA L4)
- **XTTS v2**: `infra/xtts/manifests.yaml` (Coqui XTTS streaming server)
- **Fish Speech**: `infra/fish-speech/manifests.yaml` (Fish S2 Pro with torch.compile)

## Audio Samples (Voice ID game)

Audio clips live in SQLite (`apps/voice-id/data/voiceid.db`, `clips` table) with columns:
- `is_ai` — 1 for AI-generated, 0 for human
- `language` — ISO code (en, cs, sk)
- `source` — TTS model name for AI clips (fish, xtts, moss)
- `speaker` — human speaker name
- `audio_data` — raw audio bytes (BLOB)

### Adding Samples

Drop audio files into `apps/voice-id/samples/`:

```
samples/
  ai/
    en/
      fish-anton-hello.wav       -> source=fish, speaker=anton
      xtts-anton-greeting.wav    -> source=xtts, speaker=anton
    cs/
      fish-anton-pozdrav.wav
  natural/
    en/
      anton-hello.wav            -> speaker=anton
```

Then run: `cd apps/voice-id && python3 load_samples.py`

Filename convention: `{model}-{speaker}-{description}.{ext}` for AI, `{speaker}-{description}.{ext}` for natural.

## Container Images

- **Registry**: `quay.io/agiertli/`
- **Tag policy**: Semantic versioning (v1.0.0, v1.1.0, etc.). Never use `latest`.
- **Build**: `cd apps/<app> && podman build --platform linux/amd64 -t quay.io/agiertli/<app>:v1.0.0 .`

## Managing Clips on the Running Pod (Voice ID)

**NEVER use `oc cp` to copy the database to/from the pod.** This corrupts SQLite WAL and breaks the running app.

Instead, use the upload API to add clips. Every `/api/admin/*` endpoint needs the admin token (Helm value `adminToken`) as `Authorization: Bearer`:
```bash
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
read -r -s -p 'Admin password: ' VOICEID_ADMIN_TOKEN; printf '\n'
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/upload" \
  -F "audio=@$HOME/voice-id-audio/samples/ai/en/fish-anton-hello.wav" \
  --form-string "is_ai=1" --form-string "language=en" \
  --form-string "source=Fish Audio S2 Pro" --form-string "speaker=anton" \
  --form-string "filename=ai/en/fish-anton-hello.wav"
```

To delete a clip: `curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" -X DELETE "https://${ROUTE}/api/admin/delete-clip/<clip_id>"`

To list all clips: `curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/clips"`

Scoring is server-side (`app.py`: `POST /api/game/<id>/play` starts a clip's clock, `/guess` scores once per clip of the game, `/complete` is idempotent and ignores client points). See `docs/customization/scoring.md`.

## Deployment

### Voice ID
```bash
cd apps/voice-id
helm upgrade --install voice-id helm/voice-id \
  --namespace voice-id --create-namespace \
  --set image.tag=v1.0.0 \
  --set conference.name="DevConf 2026"
```

### Voice Clone Studio
```bash
helm upgrade --install voice-clone-studio apps/voice-clone-studio/helm/voice-clone-studio \
  --namespace voice-clone-studio --create-namespace \
  --set providers.xtts.url=https://xtts-v2-tts.apps.ocp.example.com \
  --set providers.fish.url=https://fish-native-tts.apps.ocp.example.com
```

## Localization (Voice ID)

UI strings are externalized in `apps/voice-id/helm/voice-id/i18n.json`, loaded into a ConfigMap, and served via `/api/i18n`. Adding a new language = edit the JSON, run `helm upgrade`. No image rebuild.

## TTS Models Used

- **Fish S2 Pro** — highest quality, hardest to detect
- **XTTS v2** — good quality, medium difficulty
- **MOSS-TTS-Nano** — Chinese-accented Czech, easiest to detect

Only Fish S2 Pro and XTTS v2 support Czech voice cloning. 9 models were tested; 7 failed. Full report: `/Users/agiertli/Documents/work/whisper/tts/TTS-VOICE-CLONING-FAILURES.md`

## Full Stack Deployment (Fresh Cluster)

### Step 1 — GPU Nodes
```bash
# Edit infra/gpu-machineset.yaml: update AMI, subnet, AZ for target region
oc apply -f infra/gpu-machineset.yaml
# Wait ~4 min for NVIDIA GPU Operator daemonsets (driver + device-plugin)
```
Use `nvidia.com/gpu.present: "true"` as nodeSelector — NOT custom labels (Machine controller resets them).

### Step 2 — TTS Models
```bash
oc new-project tts
oc apply -f infra/xtts/manifests.yaml
oc apply -f infra/fish-speech/manifests.yaml
# Fish Speech init container downloads ~9GB model weights on first start
# After pod is ready, first request triggers 257s torch.compile warmup
```

### Step 3 — Voice ID Game
```bash
cd apps/voice-id
podman build --platform linux/amd64 -t quay.io/agiertli/voice-id:<tag> .
podman push quay.io/agiertli/voice-id:<tag>
helm upgrade --install voice-id helm/voice-id -n voice-id --create-namespace --set image.tag=<tag>
# Upload audio samples via admin API (see "Managing Clips" above)
```

### Step 4 — Voice Clone Studio
```bash
cd apps/voice-clone-studio
podman build --platform linux/amd64 -t quay.io/agiertli/voice-clone-studio:<tag> .
podman push quay.io/agiertli/voice-clone-studio:<tag>
XTTS_ROUTE=$(oc get route xtts-v2 -n tts -o jsonpath='{.spec.host}')
FISH_ROUTE=$(oc get route fish-native -n tts -o jsonpath='{.spec.host}')
helm upgrade --install voice-clone-studio helm/voice-clone-studio \
  -n voice-clone-studio --create-namespace \
  --set providers.xtts.url=https://${XTTS_ROUTE} \
  --set providers.fish.url=https://${FISH_ROUTE}
```

## AI Battle Opponents (Voice ID)

Four AI opponents with different analysis methods. **Only Gemma uses spectrograms — never show spectrograms for other opponents.**

| ID | Model | Method | Endpoint | Difficulty |
|----|-------|--------|----------|------------|
| `gemma` | Gemma-4-12B | Vision: analyzes **spectrogram image** | `ai-analyze` | 2 stars (59.3%) |
| `qwen` | Qwen2-Audio-7B | Audio: listens to **raw WAV audio** | `ai-analyze2` | 1 star (24.4%) |
| `gary-base` | Wav2Vec2 (base) | Classifier: **audio waveform** features | `ai-analyze-detector-base` | 3 stars (67.4%) |
| `gary-finetuned` | Wav2Vec2 (fine-tuned) | Classifier: **audio waveform** features | `ai-analyze-detector` | 5 stars (91.9%) |

## Operational Gotchas

- **Fish Speech cold start**: COMPILE=1 triggers 257s torch.compile on first request. Readiness probes need high failureThreshold. Don't assume pod is broken.
- **Fish Speech speed**: 30-120s per TTS request even after warmup. Routes need `haproxy.router.openshift.io/timeout: 300s`.
- **XTTS response format**: `/tts` returns base64 WAV wrapped in JSON quotes — strip quotes, then base64-decode.
- **XTTS permissions**: Needs `COQUI_TOS_AGREED=1`, `HOME=/tmp`, `TMPDIR=/tmp`, `NUMBA_CACHE_DIR=/tmp/numba_cache`, `MPLCONFIGDIR=/tmp/mpl`, plus `workingDir: /tmp`.
- **GPU node labels**: MachineSet custom labels get reset by Machine controller. Use `nvidia.com/gpu.present: "true"` (auto-set by GPU feature discovery).
- **SQLite corruption**: NEVER `oc cp` the database. Use admin upload API.
- **Fish Speech references**: Stored in-memory on the pod — lost on restart. Re-register after pod restarts.

## TTS API Contracts

### XTTS v2 (two-step)
1. `POST {url}/clone_speaker` — multipart `wav_file` → JSON `{speaker_embedding, gpt_cond_latent}`
2. `POST {url}/tts` — JSON `{text, language, speaker_embedding, gpt_cond_latent}` → base64 WAV in quotes

### Fish Speech S2 Pro (two-step)
1. `POST {url}/v1/references/add` — multipart `audio` + `name` + `text=""` → 200 OK
2. `POST {url}/v1/tts` — JSON `{text, reference_id, format: "wav"}` → raw WAV bytes
