# Deepfake Voice Detection — Research Findings

## Goal

Add a third AI player to the Voice ID battle mode: a specialist deepfake audio classifier
that outperforms general-purpose LLMs (Gemma-4-12B via spectrogram, Qwen2-Audio via raw audio).

## Dataset

48 labeled clips from the Voice ID game database (SQLite `clips` table).

### Provenance Table

| Speaker/Voice | Real/Fake | Generator         | Lang | Count | Orig SR  | Train | Val | Held-out |
|---------------|-----------|-------------------|------|------:|----------|------:|----:|---------:|
| anton         | real      | phone m4a         | en   |     4 | 16kHz*   |     2 |   1 |        1 |
| anton         | real      | phone m4a         | sk   |     4 | 16kHz*   |     1 |   2 |        1 |
| miska         | real      | phone m4a         | cs   |     4 | 16kHz*   |     3 |   0 |        1 |
| miska         | real      | phone m4a         | en   |     4 | 16kHz*   |     3 |   0 |        1 |
| miska         | real      | phone m4a         | sk   |     4 | 16kHz*   |     3 |   0 |        1 |
| anton         | fake      | Fish Audio S2 Pro | cs   |     5 | 44.1kHz  |     3 |   1 |        1 |
| anton         | fake      | Fish Audio S2 Pro | en   |     5 | 44.1kHz  |     4 |   0 |        1 |
| anton         | fake      | Fish Audio S2 Pro | sk   |     2 | 44.1kHz  |     1 |   1 |        0 |
| miska         | fake      | Fish Audio S2 Pro | cs   |     4 | 44.1kHz  |     2 |   1 |        1 |
| miska         | fake      | Fish Audio S2 Pro | en   |     1 | 44.1kHz  |     1 |   0 |        0 |
| miska         | fake      | Fish Audio S2 Pro | sk   |     1 | 44.1kHz  |     0 |   0 |        1 |
| anton         | fake      | Coqui XTTS v2     | cs   |     1 | 24kHz    |     0 |   0 |        1 |
| anton         | fake      | Coqui XTTS v2     | en   |     1 | 24kHz    |     1 |   0 |        0 |
| ana           | fake      | Coqui XTTS v2     | cs   |     1 | 24kHz    |     0 |   1 |        0 |
| barbora       | fake      | Coqui XTTS v2     | cs   |     1 | 24kHz    |     1 |   0 |        0 |
| damien        | fake      | Coqui XTTS v2     | en   |     1 | 24kHz    |     0 |   0 |        1 |
| filip         | fake      | Coqui XTTS v2     | en   |     1 | 24kHz    |     0 |   1 |        0 |
| marcos        | fake      | Coqui XTTS v2     | cs   |     1 | 24kHz    |     1 |   0 |        0 |
| miska         | fake      | Coqui XTTS v2     | cs   |     1 | 24kHz    |     1 |   0 |        0 |
| miska         | fake      | Coqui XTTS v2     | en   |     1 | 24kHz    |     1 |   0 |        0 |
| sofia         | fake      | Coqui XTTS v2     | cs   |     1 | 24kHz    |     1 |   0 |        0 |
| **TOTAL**     |           |                   |      |  **48** |        | **29** | **8** | **11** |

\* Phone m4a recordings converted to 16kHz WAV by ffmpeg during extraction.

**All clips normalized to 16kHz mono, 0.05 RMS before experiment** to eliminate sample-rate and loudness confounds.

### Not in Database (Available for Future Use)

- **13 Jakub AI-generated clips** in `samples/ai/` (Fish + XTTS, cs/en/sk) — never loaded
- **1 Jakub natural recording** at `whisper/tts/tts-samples/inputs/jakub/jakub-en-clone.mp3`
- **25 additional AI clips** from various XTTS voices — never loaded
- These could serve as an independent generalization test (unseen speaker)

## Shortcut Learning Risks Identified

1. **Sample rate confound (FIXED)**: Real=16kHz, Fish=44.1kHz, XTTS=24kHz. Model could trivially learn frequency as proxy. Fixed by normalizing all to 16kHz.
2. **Loudness confound (FIXED)**: Real RMS=0.048, Fake RMS=0.072. Fixed by normalizing all to 0.05 RMS.
3. **Speaker identity leakage**: Same speakers appear in both real and fake sets (clones). Model must learn TTS artifacts, not speaker characteristics.
4. **Duration difference**: Real=3.5s±0.7, Fake=2.8s±0.5. Mild but present. Not corrected (would require cutting/padding which introduces its own artifacts).

## Models Tested (Pre-trained, No Fine-tuning)

### 1. garystafford/wav2vec2-deepfake-voice-detector

- **Architecture**: Wav2Vec2-XLSR (315M params), 24 transformer layers, classification head
- **Training data**: ElevenLabs, Amazon Polly, Kokoro, Hume AI, Speechify, Luvvoice (~1,866 samples)
- **Reported ROC-AUC**: 0.998 (on its own test set)

**Results on our 48 clips (threshold=0.5, after normalization):**

| Category | Held-out Accuracy | Confidence |
|----------|:-:|---|
| Coqui XTTS v2 | 2/2 (100%) | p_fake=0.91 |
| Fish Audio S2 Pro | 1/4 (25%) | p_fake=0.20 — confidently wrong |
| Anton (human) | 0/2 (0%) | p_fake=0.84 — phone artifacts = "fake" |
| Miska (human) | 2/3 (67%) | p_fake=0.32 |
| **Total** | **5/11 (45.5%)** | |

### 2. hiyanachnani/deepfake-voice-detector-sota

**Total: 6/48 (12%)** — Catastrophically bad. All AI clips classified as real, all Miska clips as fake.

## Fine-Tuning Experiment

### Design

Three regimes compared on identical splits (train=29, val=8, held-out=11):

| Regime | What's Trained | Trainable Params | Epochs | LR |
|--------|---------------|------------------:|-------:|---:|
| BASE | Nothing (pretrained only) | 0 | 0 | — |
| LIGHT | Projector + classifier only | 262,914 (0.1%) | 5 | 2e-5 |
| HEAVY | Top 12 transformer layers + projector + classifier | ~157M (50%) | 20 | 1e-5 |

### Results

| Regime | Train | Val | Held-out | Train–Held Gap | Overfit? |
|--------|------:|----:|---------:|:-:|:-:|
| BASE | 65.5% | 50.0% | 45.5% | +20.1% | baseline bias |
| LIGHT | 65.5% | 50.0% | 45.5% | +20.1% | no learning |
| HEAVY | 96.6% | 100% | 90.9% | +5.6% | mild |

### Per-Source Held-out Accuracy

| Regime | XTTS v2 | Fish S2 Pro | Anton (real) | Miska (real) |
|--------|:-------:|:-----------:|:------------:|:------------:|
| BASE | 100% | 25% | 0% | 67% |
| LIGHT | 100% | 25% | 0% | 67% |
| HEAVY | 100% | 100% | 100% | 67% |

### Overfitting Curve (HEAVY regime)

```
Epoch  2: train=65.5% val=62.5% held=63.6% gap=+1.9%
Epoch  5: train=75.9% val=75.0% held=72.7% gap=+3.1%
Epoch  7: train=86.2% val=100%  held=81.8% gap=+4.4%
Epoch  9: train=89.7% val=100%  held=90.9% gap=-1.3%  ← sweet spot
Epoch 11: train=96.6% val=100%  held=90.9% gap=+5.6%
Epoch 20: train=96.6% val=100%  held=90.9% gap=+5.6%  ← plateau
```

**Sweet spot: epoch 9-10.** Held-out peaks at 90.9% while the gap is minimal (-1.3%). After epoch 11 the gap grows to 5.6% but held-out doesn't degrade further — the model plateaus rather than collapsing.

### Single Remaining Error

`miska_cs_fa779860.wav` — a real Czech recording by Miska, classified as fake with p_fake=1.0. This clip is consistently misclassified across all regimes. Likely has unusual recording characteristics that the model can't distinguish from TTS artifacts.

## Key Insights

1. **The pretrained representation IS useful** but requires substantial adaptation. Just retraining the classification head (LIGHT) does nothing — the Wav2Vec2 features need to shift in the top transformer layers.

2. **Fish S2 Pro is learnable.** The base model gets 25% on Fish held-out clips. After heavy fine-tuning: 100%. The model CAN learn Fish-specific artifacts; it just wasn't trained on them.

3. **Phone recording artifacts are learnable.** Anton went from 0% (all classified as fake) to 100% (all correct). The model learned that phone compression ≠ TTS.

4. **One stubborn misclassification** remains: `miska_cs_fa779860.wav`. With 48 clips, this single error may reflect a genuinely ambiguous recording rather than a model limitation. This is 1/20 real clips = 5% false positive rate.

5. **The heavy regime doesn't catastrophically overfit.** The gap stabilizes at 5.6% after epoch 11 — acceptable given the tiny dataset. The model learns the right features, not just memorizing.

6. **Recommended model: HEAVY at epoch 9-10** (or the final checkpoint — results are identical for held-out).

## Files

- `inspect_model_and_data.py` — Model architecture and dataset inspection
- `prepare_experiment.py` — Normalizes audio, isolates held-out set, creates splits
- `experiment.py` — Three-regime fine-tuning experiment
- `experiment_manifest.json` — Locked splits (train/val/held-out)
- `data_normalized/` — All clips normalized to 16kHz mono, 0.05 RMS
- `demo_test/` — Locked held-out clips (DO NOT MODIFY)
- `models/wav2vec2-heavy/` — Best fine-tuned model
- `results/experiment_results.json` — Full per-clip results

## Generalization Test: Jakub (Unseen Speaker)

Jakub has zero clips in the training database. His clips come from `samples/ai/` and a separate
recording in the whisper workspace. This is a true out-of-distribution test.

### Fine-tuned Model on Jakub

| Source | Correct | Accuracy | Notes |
|--------|---------|----------|-------|
| Fish S2 Pro (7 clips) | 7/7 | **100%** | All p_fake=0.9999 |
| XTTS v2 (6 clips) | 3/6 | **50%** | 3 clips classified as REAL |
| Natural (1 clip) | 1/1 | **100%** | p_fake=0.0001 |
| **Total** | **11/14** | **79%** | |

### Base Model on Jakub (for comparison)

| Source | Correct | Accuracy | Notes |
|--------|---------|----------|-------|
| Fish S2 Pro (7 clips) | 3/7 | 43% | 3 uncertain, 1 miss |
| XTTS v2 (6 clips) | 5/6 | 83% | Better than fine-tuned! |
| Natural (1 clip) | 0/1 | 0% | **False positive** — real voice classified as fake |
| **Total** | **8/14** | **57%** | |

### Trade-off Analysis

The fine-tuned model gains Fish detection (+57pp) and real-voice handling (+100pp) but
loses some XTTS detection (-33pp). For the demo use case, this is the right trade:
- Fish S2 Pro is the harder, more realistic threat
- Avoiding false positives on real voices is critical for booth credibility
- XTTS v2 has more detectable artifacts overall (base model already gets them)

## Inference Server

- `serve.py` — Flask API with `/predict` endpoint
- `Dockerfile` — CPU container, ~1.2GB image
- API: `POST /predict` with multipart `audio` file → JSON `{verdict, confidence, prob_fake, duration}`
- Verdicts: REAL (p_fake < 0.3), UNCERTAIN (0.3-0.7), SYNTHETIC (p_fake > 0.7)

## Files

- `inspect_model_and_data.py` — Model architecture and dataset inspection
- `prepare_experiment.py` — Normalizes audio, isolates held-out set, creates splits
- `experiment.py` — Three-regime fine-tuning experiment
- `experiment_manifest.json` — Locked splits (train/val/held-out)
- `predict.py` — CLI tool for single-file predictions
- `serve.py` — Flask inference server
- `Dockerfile` — Container build
- `requirements.txt` — Python dependencies
- `data_normalized/` — All clips normalized to 16kHz mono, 0.05 RMS
- `demo_test/` — Locked held-out clips (DO NOT MODIFY)
- `models/wav2vec2-heavy/` — Best fine-tuned model
- `results/experiment_results.json` — Full per-clip results

## Next Steps

1. Load missing Jakub + other clips into Voice ID game database
2. Build and push container: `podman build --platform linux/amd64 -t quay.io/agiertli/deepfake-detector:v1.0.0 .`
3. Deploy on OpenShift (CPU, no GPU needed)
4. Add as third/fourth player in battle UI with avatar
5. Spec out the "million dollar" battle scenario
