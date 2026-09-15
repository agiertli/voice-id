# Voice ID: AI or Alive?

Interactive web game for Red Hat conference booths. Players listen to audio clips and guess whether each is AI-generated or a real human voice. Tests how well modern TTS voice cloning fools human ears.

## Quick Reference

- **Main app**: `src/app.py` (Flask — game API, leaderboard, audio serving from SQLite)
- **Frontend**: `src/templates/index.html` (single-page HTML/CSS/JS — registration, game, reveal, results)
- **TV leaderboard**: `src/templates/tv.html` (full-screen auto-refresh for booth monitor)
- **Sample loader**: `load_samples.py` (imports audio files from `samples/` directory into SQLite)
- **Helm chart**: `helm/voice-id/` (Deployment, Service, Route, PVC)

## Audio Samples

Audio clips live in SQLite (`data/voiceid.db`, `clips` table) with these columns:
- `is_ai` — 1 for AI-generated, 0 for human
- `language` — ISO code (en, cs, sk)
- `source` — TTS model name for AI clips (fish, xtts, moss)
- `speaker` — human speaker name
- `audio_data` — raw audio bytes (BLOB)

### Adding Samples

Drop audio files into the `samples/` directory:

```
samples/
  ai/
    en/
      fish-anton-hello.wav       -> source=fish, speaker=anton
      xtts-anton-greeting.wav    -> source=xtts, speaker=anton
    cs/
      fish-anton-pozdrav.wav
    sk/
      moss-anton-ahoj.wav
  natural/
    en/
      anton-hello.wav            -> speaker=anton
    cs/
      katka-pozdrav.wav          -> speaker=katka
    sk/
      anton-ahoj.wav
```

Then run: `python3 load_samples.py`

Filename convention: `{model}-{speaker}-{description}.{ext}` for AI, `{speaker}-{description}.{ext}` for natural.

## Container Image

- **Registry**: `quay.io/agiertli/voice-id`
- **Tag policy**: Semantic versioning (v1.0.0, v1.1.0, etc.). Never use `latest`.
- **Build**: `podman build --platform linux/amd64 -t quay.io/agiertli/voice-id:v1.0.0 .`
- Audio samples are NOT in the image — they're loaded into SQLite on the PVC at runtime via `load_samples.py`

## Deployment

```bash
helm upgrade --install voice-id helm/voice-id \
  --namespace voice-id --create-namespace \
  --set image.tag=v1.0.0 \
  --set conference.name="DevConf 2026"
```

## 12-Factor Compliance

- Config via env vars (GAME_SIZE, PRIZE_THRESHOLD, CONFERENCE_NAME, DATA_DIR)
- Stateless app process — all state in SQLite on PVC
- Logs to stdout only
- No config baked into the image

## Game Flow

1. Player enters name
2. Server picks 5 random clips from SQLite
3. For each clip: play audio → choose AI/Human → reveal answer + model name
4. After 5 clips: show score, rank, prize eligibility
5. Leaderboard persists across games

## TTS Models Used for AI Samples

- **Fish S2 Pro** — highest quality, hardest to detect
- **XTTS v2** — good quality, medium difficulty
- **MOSS-TTS-Nano** — Chinese-accented Czech, easiest to detect
