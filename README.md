<p align="center">
  <img src="src/static/logo.jpg" alt="Voice ID: AI or Alive?" width="600">
</p>

# Voice ID: AI or Alive?

Interactive web game for Red Hat conference booths. Players listen to audio clips and guess whether each is AI-generated or a real human voice.

## How It Works

1. Player enters their name
2. 5 random clips are preloaded (mix of AI and natural, always at least 1 of each)
3. For each clip: listen, choose "AI Generated" or "Natural Voice", see the reveal
4. Final score with prize eligibility
5. Leaderboard persists across games

## Features

- Multi-language UI (English, Czech, Slovak) with flag switcher
- Real-time waveform visualization during playback
- Mystery silhouette reveal animation (person vs robot icon)
- TV leaderboard view (`/tv`) for booth monitors
- Clips preloaded with progress bar for instant playback
- Game logging with full clip metadata for analysis

## Audio Samples

Audio clips are stored in SQLite on a PVC. They are **not** baked into the container image.

### Supplying Clips

Place audio files in the `samples/` directory following this structure:

```
samples/
  ai/                              # AI-generated clips
    en/                            # Language subdirectory (ISO code)
      fish-anton-hello.wav         # {model}-{speaker}-{description}.{ext}
      xtts-miska-greeting.wav
    cs/
      fish-anton-pozdrav.wav
      xtts-jakub-kubernetes.wav
    sk/
      fish-anton-inteligencia.wav
  natural/                         # Real human voice clips
    en/
      anton-hello.wav              # {speaker}-{description}.{ext}
      miska-greeting.wav
    cs/
      miska-pozdrav.wav
    sk/
      anton-ahoj.wav
```

**Naming convention:**
- AI clips: `{model}-{speaker}-{description}.{ext}` -- the model prefix (before the first `-`) maps to a display name (e.g., `fish` -> "Fish Audio S2 Pro", `xtts` -> "Coqui XTTS v2")
- Natural clips: `{speaker}-{description}.{ext}` -- the speaker name is extracted from the first segment

**Supported formats:** WAV, MP3, M4A, OGG, FLAC

**Loading into the database:**
```bash
python3 load_samples.py
```

**Uploading to a running deployment** (do NOT use `oc cp`):
```bash
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')

# Upload a clip
curl -sk "https://${ROUTE}/api/admin/upload" \
  -F "audio=@samples/ai/en/fish-anton-hello.wav" \
  -F "is_ai=1" -F "language=en" \
  -F "source=Fish Audio S2 Pro" -F "speaker=anton" \
  -F "filename=ai/en/fish-anton-hello.wav"

# List all clips
curl -sk "https://${ROUTE}/api/admin/clips"

# Delete a clip
curl -sk -X DELETE "https://${ROUTE}/api/admin/delete-clip/<clip_id>"
```

## Deployment

Requires OpenShift 4.14+ with Helm.

```bash
# Build (on Mac, always target amd64)
podman build --platform linux/amd64 -t quay.io/your-org/voice-id:v1.0.0 .

# Deploy
helm upgrade --install voice-id helm/voice-id \
  --namespace voice-id --create-namespace \
  --set image.tag=v1.0.0 \
  --set conference.name="Your Conference"
```

See `helm/voice-id/values.yaml` for all configurable parameters.

## Localization

UI strings live in `helm/voice-id/i18n.json`, mounted as a ConfigMap. Adding a language = edit the JSON + `helm upgrade`. No image rebuild.

## TTS Models Used

- **Fish Audio S2 Pro** -- highest quality for English; produces Russian accent on Czech/Slovak
- **Coqui XTTS v2** -- very good quality for English and Czech; does not support Slovak
- **MOSS-TTS-Nano** -- Chinese-accented Czech, easiest to detect

## Tech Stack

- **Backend:** Flask + Gunicorn, SQLite with WAL mode on PVC
- **Frontend:** Vanilla HTML/CSS/JS, Web Audio API for waveform visualization
- **Deployment:** Helm chart on OpenShift (Deployment, Service, Route, ConfigMap, PVC)
