"""
Step 1: Inspect the model architecture, dataset, and identify potential issues
before any fine-tuning.

Run: python inspect_model_and_data.py
"""

import os
import glob
import json
import warnings
warnings.filterwarnings("ignore")

import torch
import librosa
import numpy as np
from transformers import AutoFeatureExtractor, AutoModelForAudioClassification, AutoConfig
from collections import Counter

MODEL_NAME = "garystafford/wav2vec2-deepfake-voice-detector"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def inspect_model():
    print("=" * 70)
    print("MODEL INSPECTION")
    print("=" * 70)

    config = AutoConfig.from_pretrained(MODEL_NAME)
    model = AutoModelForAudioClassification.from_pretrained(MODEL_NAME)
    extractor = AutoFeatureExtractor.from_pretrained(MODEL_NAME)

    print(f"\nModel: {MODEL_NAME}")
    print(f"Architecture: {config.model_type}")
    print(f"Labels: {config.id2label}")
    print(f"Num labels: {config.num_labels}")
    print(f"Hidden size: {config.hidden_size}")
    print(f"Num hidden layers: {config.num_hidden_layers}")
    print(f"Num attention heads: {config.num_attention_heads}")

    # Count parameters
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"\nTotal parameters: {total:,}")
    print(f"Trainable parameters: {trainable:,}")

    # Feature extractor config
    print(f"\nFeature extractor sampling rate: {extractor.sampling_rate}")
    print(f"Feature extractor return_attention_mask: {extractor.return_attention_mask}")
    print(f"Feature extractor padding_value: {extractor.padding_value}")
    print(f"Feature extractor do_normalize: {extractor.do_normalize}")

    # Model structure
    print(f"\nModel structure (top-level modules):")
    for name, module in model.named_children():
        param_count = sum(p.numel() for p in module.parameters())
        trainable_count = sum(p.numel() for p in module.parameters() if p.requires_grad)
        print(f"  {name}: {param_count:,} params ({trainable_count:,} trainable)")

    # Classification head
    print(f"\nClassifier head:")
    if hasattr(model, 'classifier'):
        print(f"  {model.classifier}")
    elif hasattr(model, 'projector'):
        print(f"  projector: {model.projector}")
        if hasattr(model, 'classifier'):
            print(f"  classifier: {model.classifier}")

    # What the model learns
    print(f"\n--- What this model learns ---")
    print(f"The Wav2Vec2 encoder converts raw audio waveform into a sequence of")
    print(f"hidden representations (one per ~20ms frame). The classification head")
    print(f"pools these representations and maps them to 2 classes (real/fake).")
    print(f"")
    print(f"Gary Stafford's fine-tuning approach (per blog post):")
    print(f"  - Freezes feature extractor (CNN) and bottom 12 transformer layers")
    print(f"  - Trains top 12 transformer layers + classification head")
    print(f"  - This means the model adapts high-level speech features while")
    print(f"    keeping low-level acoustic feature extraction frozen.")

    return model, extractor


def inspect_dataset():
    print("\n" + "=" * 70)
    print("DATASET INSPECTION")
    print("=" * 70)

    clips = []
    for label in ["real", "fake"]:
        label_dir = os.path.join(DATA_DIR, label)
        for wav in glob.glob(os.path.join(label_dir, "*.wav")):
            fname = os.path.basename(wav)
            parts = fname.rsplit(".", 1)[0].split("_")

            # Parse metadata from filename: source_lang_id.wav
            source = parts[0] if len(parts) >= 3 else "unknown"
            # Handle multi-word sources like "Fish Audio S2 Pro"
            lang = parts[-2] if len(parts) >= 3 else "?"
            clip_id = parts[-1] if len(parts) >= 2 else fname

            audio, sr = librosa.load(wav, sr=None, mono=True)
            duration = len(audio) / sr

            clips.append({
                "path": wav,
                "filename": fname,
                "label": label,
                "source": "_".join(parts[:-2]) if len(parts) > 2 else parts[0],
                "language": lang,
                "clip_id": clip_id,
                "duration": duration,
                "sample_rate": sr,
                "samples": len(audio),
                "rms": float(np.sqrt(np.mean(audio ** 2))),
                "max_amplitude": float(np.max(np.abs(audio))),
            })

    print(f"\nTotal clips: {len(clips)}")
    print(f"  Real: {sum(1 for c in clips if c['label'] == 'real')}")
    print(f"  Fake: {sum(1 for c in clips if c['label'] == 'fake')}")

    # By source
    print(f"\nBy source:")
    sources = Counter((c["label"], c["source"]) for c in clips)
    for (label, source), count in sorted(sources.items()):
        print(f"  {label:4s} / {source}: {count}")

    # By language
    print(f"\nBy language:")
    langs = Counter((c["label"], c["language"]) for c in clips)
    for (label, lang), count in sorted(langs.items()):
        print(f"  {label:4s} / {lang}: {count}")

    # Duration stats
    print(f"\nDuration statistics:")
    durations = [c["duration"] for c in clips]
    print(f"  Min: {min(durations):.1f}s")
    print(f"  Max: {max(durations):.1f}s")
    print(f"  Mean: {np.mean(durations):.1f}s")
    print(f"  Median: {np.median(durations):.1f}s")

    print(f"\nDuration by label:")
    for label in ["real", "fake"]:
        d = [c["duration"] for c in clips if c["label"] == label]
        print(f"  {label}: min={min(d):.1f}s, max={max(d):.1f}s, mean={np.mean(d):.1f}s")

    # Sample rate
    print(f"\nSample rates: {Counter(c['sample_rate'] for c in clips)}")

    # RMS/loudness
    print(f"\nLoudness (RMS) by label:")
    for label in ["real", "fake"]:
        rms = [c["rms"] for c in clips if c["label"] == label]
        print(f"  {label}: min={min(rms):.4f}, max={max(rms):.4f}, mean={np.mean(rms):.4f}")

    # SHORTCUT LEARNING RISKS
    print(f"\n{'=' * 70}")
    print(f"SHORTCUT LEARNING RISK ANALYSIS")
    print(f"{'=' * 70}")

    # Duration correlation
    real_dur = [c["duration"] for c in clips if c["label"] == "real"]
    fake_dur = [c["duration"] for c in clips if c["label"] == "fake"]
    print(f"\nDuration: real={np.mean(real_dur):.1f}s±{np.std(real_dur):.1f}, "
          f"fake={np.mean(fake_dur):.1f}s±{np.std(fake_dur):.1f}")
    if abs(np.mean(real_dur) - np.mean(fake_dur)) > 1.0:
        print(f"  ⚠ WARNING: Significant duration difference between classes!")
    else:
        print(f"  ✓ Duration distributions overlap")

    # Loudness correlation
    real_rms = [c["rms"] for c in clips if c["label"] == "real"]
    fake_rms = [c["rms"] for c in clips if c["label"] == "fake"]
    print(f"\nLoudness: real={np.mean(real_rms):.4f}±{np.std(real_rms):.4f}, "
          f"fake={np.mean(fake_rms):.4f}±{np.std(fake_rms):.4f}")
    ratio = np.mean(real_rms) / np.mean(fake_rms) if np.mean(fake_rms) > 0 else 0
    if ratio > 1.5 or ratio < 0.67:
        print(f"  ⚠ WARNING: Loudness ratio={ratio:.2f} — model could learn loudness as proxy!")
    else:
        print(f"  ✓ Loudness ratio={ratio:.2f} — reasonable")

    # Speaker identity leakage
    real_speakers = set(c["source"] for c in clips if c["label"] == "real")
    fake_sources = set(c["source"] for c in clips if c["label"] == "fake")
    print(f"\nReal speakers: {real_speakers}")
    print(f"Fake sources: {fake_sources}")

    # Check if same speaker appears in both real and fake
    # (Fish clones are of the same speakers who have real recordings)
    print(f"\n--- Speaker-based leakage ---")
    print(f"  Real recordings are from: anton, miska")
    print(f"  AI clips are clones of the same speakers")
    print(f"  ⚠ This means speaker identity CANNOT be used as a feature")
    print(f"  The model must learn TTS artifacts, not speaker characteristics")

    # Grouped split recommendation
    print(f"\n{'=' * 70}")
    print(f"RECOMMENDED SPLIT STRATEGY")
    print(f"{'=' * 70}")
    print(f"""
Since speakers appear in both real and fake sets (clones), a simple
random split risks the model learning speaker-specific features from
train and applying them to test.

Recommended approach: LANGUAGE-based split
  - Train: en + sk clips
  - Validation: cs clips (subset)
  - Held-out demo test: cs clips (subset) + manually curated

Alternative: UTTERANCE-based split within each speaker×source group
  - More balanced but higher leakage risk

The safest split is by language since it ensures the model can't
memorize language-specific patterns from train and apply to test.
""")

    return clips


if __name__ == "__main__":
    model, extractor = inspect_model()
    clips = inspect_dataset()
