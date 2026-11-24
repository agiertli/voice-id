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
| `gary-base` | Gary | Hard (3 stars) | `garystafford/wav2vec2-deepfake-voice-detector` (Wav2Vec2-XLSR, not fine-tuned) | Audio classifier on **waveform features** | 67.4% | +150 |
| `gary-finetuned` | Gary 2.0 | Boss (5 stars) | Same Wav2Vec2 fine-tuned on the game's voices (model V4-A) | Audio classifier on **waveform features** | 91.9% | +250 |

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
before the first clip is shown. Results are held in the browser and revealed one by
one after the player answers.

| Opponent | Game endpoint | Backend call | Answer mapping |
|---|---|---|---|
| Teddy | `POST /api/game/<id>/ai-analyze2` | `{AI_JUDGE2_URL}/chat/completions`, `audio_url` with base64 WAV, timeout 120 s | JSON `{"guess": "ai"|"human", "reasoning"}`; unparsable text → "human" if the word appears, else "ai" |
| Iris | `POST /api/game/<id>/ai-analyze` | Server renders a 0–8 kHz magma spectrogram PNG, sends it as `image_url` to `{AI_JUDGE_URL}/chat/completions`, timeout 60 s | Same JSON contract; the spectrogram is also returned and shown to the player |
| Gary | `POST /api/game/<id>/ai-analyze-detector-base` | `{DETECTOR_BASE_URL}/predict` multipart `audio`, timeout 60 s | `SYNTHETIC` → AI; `REAL` **and `UNCERTAIN`** → human |
| Gary 2.0 | `POST /api/game/<id>/ai-analyze-detector` | `{DETECTOR_URL}/predict` | Same as Gary. Thresholds `LOW_THRESHOLD=0.3`, `HIGH_THRESHOLD=0.7` on `p_fake` |

LLM calls use `temperature 0.1` and `verify=False` (the internal KServe service
uses a cluster-internal certificate). If any call fails, the browser substitutes a
random guess for that clip; see [scoring known limitations](scoring.md#known-limitations).

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
| Gary | Deployment `deepfake-detector-base` (`quay.io/agiertli/deepfake-detector-base`) | CPU, 2 request / 4 limit | `http://deepfake-detector-base:8080` |
| Gary 2.0 | Deployment `deepfake-detector-fine-tuned` (`quay.io/agiertli/deepfake-detector:v2.0.0`, V4-A) | CPU, 2 request / 4 limit | `http://deepfake-detector-fine-tuned:8080` |

The detectors need the 4 CPU limit: with 2 CPU one clip took about 20 s, with 4
about 5 s. Because all clips are analysed before the first round, slow opponents
lengthen the loading screen, not the rounds.

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
detector:       # Gary 2.0
  enabled: true
  url: "http://deepfake-detector-fine-tuned:8080"
detectorBase:   # Gary
  enabled: true
  url: "http://deepfake-detector-base:8080"
```

```bash
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml --set aiJudge.enabled=false
```

The game does not health-check opponents before listing them: an opponent whose
URL is set but whose backend is down still appears and then plays coin flips.
Verify each one with the smoke test in [INSTALLATION.md](../INSTALLATION.md#6-verify)
before the booth opens, and disable any that are not healthy.

## Retraining Gary 2.0

When you add new speakers or a new TTS engine, Gary 2.0's accuracy on them is
unknown. Retrain with `apps/deepfake-detector/experiment_v4.py` (about 11 minutes
on an Apple M3), rebuild `quay.io/agiertli/deepfake-detector` with a new tag,
update `infra/deepfake-detector/manifests.yaml` and re-measure. Details and
caveats: `apps/deepfake-detector/FINDINGS.md`.
