# AI opponents (battle mode)

Battle mode lets a player label the same clips as an AI detector and compare
results. Four opponents exist; each appears only when its backend URL is
configured (`GET /api/battle/opponents`). If none is configured, the battle
button is hidden and only solo play is available.

**Only Iris (Gemma) uses spectrograms.** The other three work on the audio
itself. Never show or describe spectrograms for them.

| Id | Name | Difficulty | Model | Method | Measured accuracy | Bonus for beating it |
|---|---|---|---|---|---:|---:|
| `qwen` | Teddy | Easy (1 star) | Qwen2-Audio-7B-Instruct (vLLM) | Listens to the **raw WAV audio** with a forensic prompt | 24.4% | +50 |
| `gemma` | Iris | Medium (2 stars) | Gemma-4-12B, `RedHatAI/gemma-4-12B-it-FP8-dynamic` (vLLM) | Looks at a **spectrogram image** rendered by the game server | 59.3% | +100 |
| `gary-base` | Gary | Hard (3 stars) | `garystafford/wav2vec2-deepfake-voice-detector` (Wav2Vec2-XLSR, not fine-tuned), served by OpenVINO Model Server | Audio classifier on **waveform features** | 67.4% | +150 |
| `gary-finetuned` | Gary 2.0 | Boss (5 stars) | Same Wav2Vec2 fine-tuned on the game's voices (model V4-A), served by OpenVINO Model Server | Audio classifier on **waveform features** | 91.9% | +250 |

Accuracy figures come from the project's measurements on the game clip set
(`CLAUDE.md`, `apps/deepfake-detector/FINDINGS.md`). They describe that clip set,
not deepfake detection in general. Display names (Teddy, Iris, Gary, Gary 2.0) and
difficulty labels come from the redesigned UI (`tv.html` at the time of writing);
the opponent card texts are i18n strings `opponent_<id>_name|label|bio` in
`apps/voice-id/helm/voice-id/i18n.json`. Points: see [scoring](scoring.md).

## How each opponent decides

All analysis runs on the **game server**, which fetches the clip from SQLite,
converts it with ffmpeg to 22.05 kHz mono 16-bit WAV and calls the backend. The
browser calls one endpoint per clip, in parallel, during the loading screen
before the first clip is shown. The **server records** each answer for that game
(first answer wins) and the response is sealed: `{status: "ready", opponent,
clip_id, fallback}` plus the spectrogram for Iris, but no guess or reasoning. The
answer is revealed in the player's `/guess` response (`opponent_guess`,
`opponent_reasoning`, `opponent_correct`, `opponent_fallback`), so it cannot be
read ahead of time. The first analysis request fixes the game's opponent: a
different opponent in the same game, or choosing one after the first guess, gets
HTTP 409.

| Opponent | Game endpoint | Backend call | Answer mapping |
|---|---|---|---|
| Teddy | `POST /api/game/<id>/ai-analyze2` | `{AI_JUDGE2_URL}/chat/completions`, `audio_url` with base64 WAV, timeout 120 s | JSON `{"guess": "ai"|"human", "reasoning"}`; unparsable text → "human" if the word appears, else "ai" |
| Iris | `POST /api/game/<id>/ai-analyze` | Server renders a 0–8 kHz magma spectrogram PNG, sends it as `image_url` to `{AI_JUDGE_URL}/chat/completions`, timeout 60 s | Same JSON contract; the spectrogram is also returned and shown to the player |
| Gary | `POST /api/game/<id>/ai-analyze-detector-base` | `{DETECTOR_BASE_URL}/predict` multipart `audio`, timeout 60 s. The adapter resamples to 16 kHz, RMS-normalizes, keeps the **native clip length**, z-normalizes and calls OVMS (KServe v2 binary) | `SYNTHETIC` → AI; `REAL` **and `UNCERTAIN`** → human |
| Gary 2.0 | `POST /api/game/<id>/ai-analyze-detector` | `{DETECTOR_URL}/predict`; same adapter steps, but a **fixed 10 s** window (160000 samples, padded or truncated) | Same as Gary. Thresholds `LOW_THRESHOLD=0.3`, `HIGH_THRESHOLD=0.7` on `p_fake` |

LLM calls use `temperature 0.1` and `verify=False` (the internal KServe service
uses a cluster-internal certificate). If any call fails, the **server** records a
random guess for that clip (`fallback: true`, reasoning "Analysis failed"; also when
the analysis request never arrived), and the player sees that same answer; see
[scoring known limitations](scoring.md#known-limitations).

## Answer cache and background pre-warm

Model inference is what players wait for on "Preparing the game" (measured for the
5 clips of a battle, analysed in parallel, cold cache: Teddy 4 s, Iris 8 s; on the
legacy Flask/PyTorch detectors Gary 8 s and Gary 2.0 18–23 s, on OpenVINO Model Server
Gary 2.5 s and Gary 2.0 6 s). An opponent's answer depends only on the clip audio, so
the server caches it:

- **Table `opponent_cache`** (one row per opponent and clip): guess, reasoning,
  detector details (`extra`: verdict, confidence, `p_fake`), the Iris spectrogram
  PNG, and the **sha256 of the audio** it was computed from. A row is used only
  while it matches the clip's current `clips.audio_sha256`. Replacing a clip's audio
  (admin upload, seed pack import in replace mode, the on-the-fly WAV transcode, or
  any direct database update, through an SQLite trigger) makes its rows stale.
  Deleting a clip deletes its rows.
- **On a battle request** the server checks the cache first. A hit is recorded on
  the round exactly as if the model had just answered: still sealed, still first
  answer wins, the same answer revealed at `/guess`. A miss calls the model as
  before and stores the answer. **Coin-flip fallbacks (model failures) are never
  cached.** They stay per game, and the next request or warm run asks the model again.
- **Background pre-warm**: every app process starts one daemon thread. A database
  lease row makes sure that only one process (of the 4 gunicorn workers) warms at a
  time. The holder analyses every clip missing from the cache, **one request at a
  time per opponent**, so live games are not slowed. It repeats every
  `OPPONENT_CACHE_WARM_INTERVAL_SEC` (default 600 s) to pick up new or changed clips,
  and on demand. Three consecutive errors pause that opponent until the next run.
  A full warm takes roughly clips × per-call time per opponent, and the opponents
  warm in parallel: Gary 2.0 on OVMS about 1.2 s per clip (129 clips ≈ 3 min), Iris
  4–5 s per clip (≈ 10 min), so Iris usually finishes last.
- **Readiness step**: after every install, seed pack import or bulk upload, trigger a
  warm run and wait until every opponent shows `cached == total` (`install.sh
  --wait-cache` does this). Then the pre-game prep takes about 1 s.
- **Result**: once warm, the battle pre-analysis returns in well under a second. The
  browser is unchanged; it calls the same endpoints.
- Cached answers are a snapshot. After a detector backend switch (`detectorBackend`),
  retraining or swapping a model (for example a new Gary 2.0 IR), clear that
  opponent's rows (`POST /api/admin/opponent-cache/clear` with `{"opponent": ...}`) and
  warm again, so its answers come from the new backend. (The OpenVINO switch was
  parity-tested with identical verdicts, so clearing was optional there.)

| Endpoint (admin token) | Effect |
|---|---|
| `GET /api/admin/opponent-cache/status` | Per opponent: `cached`/`total`, last run start/finish, clips analysed and errors in the last run, last error; whether a warm run holds the lease |
| `POST /api/admin/opponent-cache/warm` | Queue a warm run now (`202`; picked up within about 5 s) |
| `POST /api/admin/opponent-cache/clear` | Delete all cached answers, or one opponent's with JSON `{"opponent": "gary-finetuned"}` |

The admin page **Clips & seed packs** tab shows the same status in one line
(Teddy 109/109 · Iris 109/109 · …) with a **Warm now** button.

| Helm value | Env var | Default | Effect |
|---|---|---|---|
| `opponentCache.enabled` | `OPPONENT_CACHE_ENABLED` | `true` | `false` turns off lookups, storing and pre-warm (every battle calls the models live, as before) |
| `opponentCache.warmIntervalSec` | `OPPONENT_CACHE_WARM_INTERVAL_SEC` | `600` | Seconds between warm runs (minimum 30) |

```bash
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
read -r -s -p 'Admin password: ' VOICEID_ADMIN_TOKEN; printf '\n'
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/opponent-cache/status"
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" -X POST "https://${ROUTE}/api/admin/opponent-cache/clear" \
  -H 'Content-Type: application/json' -d '{"opponent": "gary-finetuned"}'
```

## Character notes for facilitators

- **Teddy** almost always says "human". It loses to anyone who guesses AI often.
- **Iris** is paranoid: it calls many real voices AI because phone recordings
  look "clean" or "noisy" in unexpected ways on a spectrogram. Showing its
  spectrogram is part of the fun and makes the point that an image of sound is a
  weak signal.
- **Gary** was trained on commercial TTS (ElevenLabs, Polly and others) and never
  saw Fish S2 Pro; it misses many Fish clips and flags phone recordings as fake.
- **Gary 2.0** was fine-tuned on this booth's own voices. It rarely accuses a real
  person (false positive rate about 3.3%). Its known blind spot is XTTS
  `clone_speaker` output of speakers it has not seen; accepted for the demo
  because the game's clips come from known speakers.

The message for visitors: a general-purpose multimodal model is a poor deepfake
detector; a small specialised classifier trained on the right data is much
better, but only for the voices and generators it was trained on.

## Where they run

| Opponent | Workload (namespace `voice-id`) | Hardware | Internal URL configured in values.yaml |
|---|---|---|---|
| Teddy | LLMInferenceService `qwen2-audio` | 1× NVIDIA L4 | `https://qwen2-audio-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1` |
| Iris | LLMInferenceService `gemma` | 1× NVIDIA L4 (12.75 GiB weights, FP8) | `https://gemma-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1` |
| Gary | InferenceService `gary-base` on OpenShift AI's built-in OpenVINO Model Server (`kserve-ovms`, FP32 IR on PVC) behind Deployment `adapter-gary-base` | CPU, predictor 2 request / 4 limit, 4 / 6 Gi | `http://adapter-gary-base.voice-id.svc.cluster.local:8080` |
| Gary 2.0 | InferenceService `gary-finetuned` (V4-A, FP32 IR) behind Deployment `adapter-gary-finetuned` | CPU, predictor 2 request / 4 limit, 4 / 4 Gi | `http://adapter-gary-finetuned.voice-id.svc.cluster.local:8080` |

Red Hat OpenShift AI serves all four: the generative models with vLLM on GPUs, the
predictive classifiers with OpenVINO Model Server on CPU. The adapters keep the
detector HTTP API (`/predict`, `/health`, `/ready`) and do the audio preprocessing;
OVMS only runs the network. On OVMS one clip takes about 1.2 s (Gary 2.0) and
0.4–0.8 s (Gary) at the 4-CPU limit, 2.2–3.3x faster than the decommissioned
Flask/PyTorch Deployments, with identical verdicts on all 129 live clips. Because all
clips are analysed before the first round, slow opponents lengthen the loading
screen, not the rounds. Details: [OPENVINO-DETECTOR.md](../OPENVINO-DETECTOR.md).

## Enable or disable an opponent

Each opponent is a Helm values block. Disabling removes the env var and the
opponent disappears from the selection screen. No image rebuild.

```yaml
aiJudge:        # Iris (gemma)
  enabled: true
  url: "https://gemma-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1"
  model: "gemma-4-12b"            # must equal spec.model.name of the LLMInferenceService
aiJudge2:       # Teddy (qwen)
  enabled: true
  url: "https://qwen2-audio-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1"
  model: "Qwen2-Audio-7B-Instruct"  # must equal the served model name
detector:       # Gary 2.0 (enabled flag; url is used only with detectorBackend=legacy)
  enabled: true
  url: "http://deepfake-detector-fine-tuned:8080"
detectorBase:   # Gary
  enabled: true
  url: "http://deepfake-detector-base:8080"
detectorBackend: openvino   # default; legacy = emergency rollback to the Flask/PyTorch detectors
openvino:
  detectorUrl: "http://adapter-gary-finetuned.voice-id.svc.cluster.local:8080"
  detectorBaseUrl: "http://adapter-gary-base.voice-id.svc.cluster.local:8080"
```

```bash
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml --set aiJudge.enabled=false
```

The game's opponent picker and the `/tv` opponent list (refresh every 10 s) both
read `/api/battle/opponents`, so disabled opponents disappear everywhere. The
`minimal` install profile disables Teddy and Iris (no GPUs); see
[install profiles](../INSTALLATION.md#install-profiles).

The game does not health-check opponents before listing them: an opponent whose
URL is set but whose backend is down still appears and then plays coin flips.
Verify each one with the smoke test in [INSTALLATION.md](../INSTALLATION.md#6-verify)
before the booth opens, and disable any that are not healthy.

## Retraining Gary 2.0

When you add new speakers or a new TTS engine, Gary 2.0's accuracy on them is
unknown. Retrain with `apps/deepfake-detector/experiment_v4.py` (about 11 minutes
on an Apple M3), export the checkpoint to an FP32 OpenVINO IR with
`apps/deepfake-detector/export_openvino.py`, load it onto the `gary-finetuned-models`
PVC ([OPENVINO-DETECTOR.md](../OPENVINO-DETECTOR.md#3-deployment)), restart the
predictor, clear Gary 2.0's answer cache and warm it, then re-measure. If the new
model needs different preprocessing, change the adapter (`INPUT_LENGTH` in
`infra/openvino-detector/adapter.yaml`, or `apps/deepfake-detector/ovms_adapter/`).
Details and caveats: `apps/deepfake-detector/FINDINGS.md`.

Iris's spectrogram is analysed by Gemma as a full-quality PNG, but sent to the browser as a compressed WebP display copy (~10x smaller, e.g. 387 KB -> ~50 KB), so battle preparation stays fast on booth Wi-Fi. Measured on the live cluster with a warm cache (v3.4.1): pre-game analysis of 5 clips takes ~0.4-1.4 s for every opponent (was 4-23 s).

**When the pre-warm runs:** in the background, never blocking the pod. The warmer thread starts with the first request each server process receives (in practice the Kubernetes health probe, seconds after startup); one process at a time warms (DB lease). It runs every `opponentCache.warmIntervalSec` (default 600 s), immediately on **Warm now** / `POST /api/admin/opponent-cache/warm`, and automatically right after an admin upload or a seed pack import that adds or replaces clips. Each run only computes missing or changed (opponent, clip) answers; the cache lives in the game DB on the PVC, so restarts and upgrades keep it.
