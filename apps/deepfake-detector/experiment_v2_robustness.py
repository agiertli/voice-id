"""
Experiment v2 Robustness: Systematic shortcut and robustness investigation.

Steps:
  1. Train Heavy model WITH amplitude augmentation
  2. Evaluate all models under controlled conditions:
     - Original data
     - Silence-standardized data
     - Amplitude-varied data
     - Combined silence + amplitude controlled
  3. Jakub OOD evaluation with all models
  4. XTTS probability shift diagnostic

Models evaluated:
  - Base (pretrained garystafford)
  - Heavy (current fine-tuned, no augmentation)
  - Heavy+Aug (new fine-tuned WITH amplitude augmentation)

Usage:
    python experiment_v2_robustness.py --device cpu
    python experiment_v2_robustness.py --device mps --skip-training  # eval only
"""

import os
import json
import copy
import glob
import argparse
import warnings
import time
import random as stdlib_random
from collections import defaultdict

warnings.filterwarnings("ignore")

import numpy as np
import librosa
import soundfile as sf
import torch
from torch.utils.data import Dataset
from transformers import (
    AutoFeatureExtractor,
    AutoModelForAudioClassification,
    Trainer,
    TrainingArguments,
)
from sklearn.metrics import accuracy_score, balanced_accuracy_score, roc_auc_score, confusion_matrix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(BASE_DIR, "experiment_manifest.json")
RESULTS_DIR = os.path.join(BASE_DIR, "results", "v2")
MODELS_DIR = os.path.join(BASE_DIR, "models")
SAMPLES_DIR = os.path.join(BASE_DIR, "..", "voice-id", "samples")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"

SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10
TARGET_RMS = 0.05

AMPLITUDE_GAINS = [0.5, 0.75, 1.0, 1.5, 2.0, 2.5]


# ============================================================
# Unified preprocessing (matches experiment_v2.py exactly)
# ============================================================

def normalize_rms(audio, target_rms=TARGET_RMS):
    current_rms = np.sqrt(np.mean(audio ** 2))
    if current_rms > 1e-6:
        audio = audio * (target_rms / current_rms)
    return np.clip(audio, -1.0, 1.0)


def preprocess_audio(audio):
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)
    if len(audio) > MAX_LENGTH:
        audio = audio[:MAX_LENGTH]
    elif len(audio) < MAX_LENGTH:
        audio = np.pad(audio, (0, MAX_LENGTH - len(audio)))
    return audio


def extract_features_for_model(audio, feature_extractor):
    return feature_extractor(
        audio, sampling_rate=SAMPLE_RATE, return_tensors="pt",
        padding="max_length", max_length=MAX_LENGTH, truncation=True,
    )


# ============================================================
# Silence standardization
# ============================================================

def trim_silence(audio, top_db=30):
    """Trim leading and trailing silence."""
    trimmed, _ = librosa.effects.trim(audio, top_db=top_db)
    return trimmed


def standardize_silence(audio, sr=SAMPLE_RATE, lead_ms=20, trail_ms=20):
    """Trim all silence then add fixed lead/trail silence to both real and fake."""
    trimmed = trim_silence(audio)
    lead_samples = int(lead_ms / 1000 * sr)
    trail_samples = int(trail_ms / 1000 * sr)
    return np.concatenate([
        np.zeros(lead_samples, dtype=np.float32),
        trimmed,
        np.zeros(trail_samples, dtype=np.float32),
    ])


def compute_silence_stats(audio, sr=SAMPLE_RATE, silence_thresh_db=-40):
    """Quick silence stats for reporting."""
    frame_length = int(0.025 * sr)
    hop_length = int(0.010 * sr)
    frames = librosa.util.frame(audio, frame_length=frame_length, hop_length=hop_length)
    frame_energy = np.sum(frames ** 2, axis=0)
    frame_energy_db = 10 * np.log10(frame_energy + 1e-10)
    is_speech = frame_energy_db > silence_thresh_db
    n_frames = len(is_speech)
    if n_frames == 0:
        return {"vad_ratio": 0, "leading_silence_ms": 0, "trailing_silence_ms": 0}

    first_speech = 0
    for i in range(n_frames):
        if is_speech[i]:
            first_speech = i
            break
    last_speech = n_frames
    for i in range(n_frames - 1, -1, -1):
        if is_speech[i]:
            last_speech = i + 1
            break

    return {
        "vad_ratio": float(np.mean(is_speech)),
        "leading_silence_ms": float(first_speech * hop_length / sr * 1000),
        "trailing_silence_ms": float((n_frames - last_speech) * hop_length / sr * 1000),
    }


# ============================================================
# Audio dataset with amplitude augmentation
# ============================================================

class AugmentedAudioDataset(Dataset):
    """Training dataset with optional amplitude augmentation."""

    def __init__(self, clips, feature_extractor, augment=False,
                 gain_range=(0.5, 2.0)):
        self.clips = clips
        self.feature_extractor = feature_extractor
        self.augment = augment
        self.gain_range = gain_range

    def __len__(self):
        return len(self.clips)

    def __getitem__(self, idx):
        clip = self.clips[idx]
        audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)

        if self.augment:
            gain = np.random.uniform(self.gain_range[0], self.gain_range[1])
            audio = audio * gain
            audio = np.clip(audio, -1.0, 1.0)

        audio = preprocess_audio(audio)
        inputs = extract_features_for_model(audio, self.feature_extractor)
        item = {k: v.squeeze(0) for k, v in inputs.items()}
        item["labels"] = torch.tensor(clip["label"], dtype=torch.long)
        return item


# ============================================================
# Model evaluation
# ============================================================

def evaluate_clips(model, feature_extractor, clips, device, audio_transform=None):
    """Evaluate model on clips with optional audio transform."""
    model.eval()
    results = []

    for clip in clips:
        audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)

        if audio_transform:
            audio = audio_transform(audio)

        audio = preprocess_audio(audio)
        inputs = extract_features_for_model(audio, feature_extractor)
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)
            prob_fake = probs[0][1].item()

        results.append({
            "filename": clip["filename"],
            "label": clip["label"],
            "source": clip.get("source", "unknown"),
            "language": clip.get("language", "?"),
            "prob_fake": float(prob_fake),
        })

    return results


def summarize_results(results, threshold=0.5):
    """Compute summary metrics."""
    if not results:
        return {"accuracy": 0, "total": 0}

    labels = np.array([r["label"] for r in results])
    probs = np.array([r["prob_fake"] for r in results])
    preds = (probs >= threshold).astype(int)

    acc = accuracy_score(labels, preds)
    bal_acc = balanced_accuracy_score(labels, preds)

    real_mask = labels == 0
    fake_mask = labels == 1
    real_correct = int(np.sum(preds[real_mask] == labels[real_mask])) if real_mask.any() else 0
    real_total = int(real_mask.sum())
    fake_correct = int(np.sum(preds[fake_mask] == labels[fake_mask])) if fake_mask.any() else 0
    fake_total = int(fake_mask.sum())

    try:
        auc = roc_auc_score(labels, probs)
    except ValueError:
        auc = None

    by_source = defaultdict(lambda: {"correct": 0, "total": 0, "probs": []})
    for r in results:
        pred = 1 if r["prob_fake"] >= threshold else 0
        by_source[r["source"]]["total"] += 1
        if pred == r["label"]:
            by_source[r["source"]]["correct"] += 1
        by_source[r["source"]]["probs"].append(r["prob_fake"])

    return {
        "accuracy": acc,
        "balanced_accuracy": bal_acc,
        "roc_auc": auc,
        "total_correct": int(np.sum(preds == labels)),
        "total": len(labels),
        "real_correct": real_correct,
        "real_total": real_total,
        "fake_correct": fake_correct,
        "fake_total": fake_total,
        "mean_prob_fake_real": float(np.mean(probs[real_mask])) if real_mask.any() else None,
        "mean_prob_fake_fake": float(np.mean(probs[fake_mask])) if fake_mask.any() else None,
        "by_source": {k: {"correct": v["correct"], "total": v["total"],
                          "accuracy": v["correct"]/v["total"],
                          "mean_pfake": float(np.mean(v["probs"]))}
                      for k, v in sorted(by_source.items())},
    }


# ============================================================
# Training with amplitude augmentation
# ============================================================

def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {"accuracy": (preds == labels).mean()}


def train_heavy_with_augmentation(manifest, device, feature_extractor):
    """Train Heavy model with amplitude augmentation."""
    print("\n" + "=" * 70)
    print("STEP 1: Training Heavy + Amplitude Augmentation")
    print("=" * 70)

    model = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    model.to(device)

    # Freeze same layers as original Heavy: CNN + bottom 12 transformers
    for param in model.wav2vec2.feature_extractor.parameters():
        param.requires_grad = False
    for param in model.wav2vec2.feature_projection.parameters():
        param.requires_grad = False
    for i in range(12):
        for param in model.wav2vec2.encoder.layers[i].parameters():
            param.requires_grad = False

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"  Trainable: {trainable:,} / {total:,} ({100*trainable/total:.1f}%)")

    train_dataset = AugmentedAudioDataset(
        manifest["train"], feature_extractor,
        augment=True, gain_range=(0.5, 2.5),
    )
    val_dataset = AugmentedAudioDataset(
        manifest["val"], feature_extractor,
        augment=False,
    )

    output_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy-augmented")

    training_args = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=20,
        per_device_train_batch_size=2,
        per_device_eval_batch_size=2,
        learning_rate=1e-5,
        weight_decay=0.01,
        warmup_steps=2,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        logging_steps=100,
        fp16=False,
        dataloader_num_workers=0,
        use_cpu=(str(device) == "cpu"),
        report_to="none",
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        compute_metrics=compute_metrics,
    )

    print("  Training 20 epochs with gain augmentation (0.5x-2.5x)...")
    trainer.train()

    # Save
    model.save_pretrained(output_dir)
    feature_extractor.save_pretrained(output_dir)
    print(f"  Model saved to {output_dir}")

    return model


# ============================================================
# Find and prepare Jakub clips
# ============================================================

def find_jakub_clips():
    """Find all Jakub clips in the samples directory."""
    clips = []
    samples_base = os.path.join(BASE_DIR, "..", "voice-id", "samples")

    for subdir in ["ai/cs", "ai/en", "ai/sk"]:
        search_dir = os.path.join(samples_base, subdir)
        if not os.path.isdir(search_dir):
            continue
        for f in sorted(os.listdir(search_dir)):
            if "jakub" in f.lower() and f.endswith((".wav", ".mp3", ".m4a")):
                # Parse source from filename: {model}-{speaker}-{desc}.ext
                parts = f.rsplit(".", 1)[0].split("-")
                source = parts[0] if parts else "unknown"
                source_map = {"fish": "Fish Audio S2 Pro", "xtts": "Coqui XTTS v2"}
                lang = subdir.split("/")[-1]
                clips.append({
                    "path": os.path.join(search_dir, f),
                    "filename": f,
                    "label": 1,
                    "label_name": "fake",
                    "source": source_map.get(source, source),
                    "language": lang,
                    "speaker": "jakub",
                })

    # Check for natural Jakub recordings
    for subdir in ["natural/cs", "natural/en", "natural/sk"]:
        search_dir = os.path.join(samples_base, subdir)
        if not os.path.isdir(search_dir):
            continue
        for f in sorted(os.listdir(search_dir)):
            if "jakub" in f.lower() and f.endswith((".wav", ".mp3", ".m4a")):
                lang = subdir.split("/")[-1]
                clips.append({
                    "path": os.path.join(search_dir, f),
                    "filename": f,
                    "label": 0,
                    "label_name": "real",
                    "source": "jakub",
                    "language": lang,
                    "speaker": "jakub",
                })

    return clips


# ============================================================
# Main evaluation pipeline
# ============================================================

def print_summary(label, summary, per_clip=None, threshold=0.5):
    """Print evaluation summary."""
    print(f"\n  [{label}]")
    print(f"  Accuracy: {summary['total_correct']}/{summary['total']} = {summary['accuracy']:.1%}")
    if summary.get("roc_auc") is not None:
        print(f"  ROC-AUC: {summary['roc_auc']:.3f}")
    if summary['real_total'] > 0:
        print(f"  REAL: {summary['real_correct']}/{summary['real_total']} "
              f"(mean p_fake={summary['mean_prob_fake_real']:.4f})")
    if summary['fake_total'] > 0:
        print(f"  FAKE: {summary['fake_correct']}/{summary['fake_total']} "
              f"(mean p_fake={summary['mean_prob_fake_fake']:.4f})")
    for src, stats in sorted(summary["by_source"].items()):
        print(f"    {src:25s}: {stats['correct']}/{stats['total']} = {stats['accuracy']:.0%} "
              f"(mean p_fake={stats['mean_pfake']:.4f})")
    if per_clip:
        for r in per_clip:
            pred = "FAKE" if r["prob_fake"] >= threshold else "REAL"
            actual = "FAKE" if r["label"] == 1 else "REAL"
            mark = "+" if pred == actual else "X"
            print(f"    {mark} {r['filename'][:45]:45s} {actual:4s}→{pred:4s} p_fake={r['prob_fake']:.4f}")


def run_evaluation_matrix(models_dict, manifest, jakub_clips, device, feature_extractor):
    """Step 2-4: Run all evaluations across conditions."""
    print("\n" + "=" * 70)
    print("EVALUATION MATRIX")
    print("=" * 70)

    # Define audio transforms
    def make_amplitude_transform(gain):
        def transform(audio):
            return np.clip(audio * gain, -1.0, 1.0)
        return transform

    def silence_transform(audio):
        return standardize_silence(audio)

    def combined_transform(gain):
        def transform(audio):
            audio = standardize_silence(audio)
            return np.clip(audio * gain, -1.0, 1.0)
        return transform

    def normalize_transform(audio):
        return normalize_rms(audio, TARGET_RMS)

    # Combine train + val for broader evaluation (NOT locked test)
    eval_clips = manifest["train"] + manifest["val"]

    all_results = {}

    for model_name, model in models_dict.items():
        print(f"\n{'━' * 60}")
        print(f"MODEL: {model_name}")
        print(f"{'━' * 60}")

        model_results = {}

        # --- Condition 1: Original ---
        print("\n  ── Original (no transform) ──")
        res = evaluate_clips(model, feature_extractor, eval_clips, device)
        summary = summarize_results(res)
        print_summary("Train+Val Original", summary)
        model_results["original"] = {"summary": summary, "per_clip": res}

        # --- Condition 2: Silence-standardized ---
        print("\n  ── Silence-standardized (20ms lead/trail) ──")

        # Report silence stats before/after
        print("  Silence stats BEFORE standardization:")
        real_lead, fake_lead = [], []
        for clip in eval_clips[:5]:  # sample
            audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)
            stats = compute_silence_stats(audio)
            if clip["label"] == 0:
                real_lead.append(stats["leading_silence_ms"])
            else:
                fake_lead.append(stats["leading_silence_ms"])

        res_sil = evaluate_clips(model, feature_extractor, eval_clips, device,
                                 audio_transform=silence_transform)
        summary_sil = summarize_results(res_sil)
        print_summary("Train+Val Silence-Std", summary_sil)
        model_results["silence_standardized"] = {"summary": summary_sil}

        # --- Condition 3: Amplitude variation ---
        print("\n  ── Amplitude variation ──")
        amp_results = {}
        for gain in AMPLITUDE_GAINS:
            res_amp = evaluate_clips(model, feature_extractor, eval_clips, device,
                                     audio_transform=make_amplitude_transform(gain))
            summary_amp = summarize_results(res_amp)
            print(f"  Gain {gain:.1f}x: {summary_amp['total_correct']}/{summary_amp['total']} = "
                  f"{summary_amp['accuracy']:.1%}  "
                  f"REAL p_fake={summary_amp['mean_prob_fake_real']:.4f}  "
                  f"FAKE p_fake={summary_amp['mean_prob_fake_fake']:.4f}")
            amp_results[f"gain_{gain}"] = summary_amp
        model_results["amplitude_variation"] = amp_results

        # --- Condition 4: Combined silence + amplitude ---
        print("\n  ── Combined: silence-std + amplitude variation ──")
        combined_results = {}
        for gain in [0.5, 1.0, 2.0, 2.5]:
            res_comb = evaluate_clips(model, feature_extractor, eval_clips, device,
                                       audio_transform=combined_transform(gain))
            summary_comb = summarize_results(res_comb)
            print(f"  Sil+Gain {gain:.1f}x: {summary_comb['total_correct']}/{summary_comb['total']} = "
                  f"{summary_comb['accuracy']:.1%}  "
                  f"REAL p_fake={summary_comb['mean_prob_fake_real']:.4f}  "
                  f"FAKE p_fake={summary_comb['mean_prob_fake_fake']:.4f}")
            combined_results[f"silgain_{gain}"] = summary_comb
        model_results["combined"] = combined_results

        # --- Jakub OOD (with RMS normalization to match training) ---
        if jakub_clips:
            print("\n  ── Jakub OOD (RMS-normalized) ──")
            res_jakub = evaluate_clips(model, feature_extractor, jakub_clips, device,
                                        audio_transform=normalize_transform)
            summary_jakub = summarize_results(res_jakub)
            print_summary("Jakub OOD (normalized)", summary_jakub, per_clip=res_jakub)
            model_results["jakub_normalized"] = {"summary": summary_jakub, "per_clip": res_jakub}

            print("\n  ── Jakub OOD (raw, no normalization) ──")
            res_jakub_raw = evaluate_clips(model, feature_extractor, jakub_clips, device)
            summary_jakub_raw = summarize_results(res_jakub_raw)
            print_summary("Jakub OOD (raw)", summary_jakub_raw, per_clip=res_jakub_raw)
            model_results["jakub_raw"] = {"summary": summary_jakub_raw, "per_clip": res_jakub_raw}

        all_results[model_name] = model_results

    return all_results


def run_xtts_probability_shift(models_dict, jakub_clips, device, feature_extractor):
    """Step 6: XTTS probability shift diagnostic."""
    print("\n" + "=" * 70)
    print("STEP 6: XTTS PROBABILITY SHIFT DIAGNOSTIC")
    print("=" * 70)

    xtts_clips = [c for c in jakub_clips if "XTTS" in c.get("source", "")]
    if not xtts_clips:
        print("  No XTTS clips found in Jakub data.")
        return {}

    def normalize_transform(audio):
        return normalize_rms(audio, TARGET_RMS)

    print(f"\n  {len(xtts_clips)} XTTS clips from Jakub (unseen speaker)")
    print(f"\n  {'Clip':45s}", end="")
    for mname in models_dict:
        print(f" {mname:>12s}", end="")
    print()
    print("  " + "-" * (45 + 13 * len(models_dict)))

    shift_data = []
    for clip in xtts_clips:
        row = {"filename": clip["filename"]}
        print(f"  {clip['filename'][:45]:45s}", end="")

        for mname, model in models_dict.items():
            # Evaluate with normalization (fair comparison)
            res = evaluate_clips(model, feature_extractor, [clip], device,
                                 audio_transform=normalize_transform)
            pfake = res[0]["prob_fake"]
            row[mname] = pfake
            pred = "FAKE" if pfake >= 0.5 else "REAL"
            marker = "+" if pred == "FAKE" else "X"  # FAKE is correct for these clips
            print(f" {pfake:>7.4f} ({marker})", end="")

        print()
        shift_data.append(row)

    # Interpretation
    print("\n  Interpretation:")
    if "base" in models_dict and "heavy" in models_dict:
        for row in shift_data:
            base_p = row.get("base", 0)
            heavy_p = row.get("heavy", 0)
            if base_p >= 0.5 and heavy_p < 0.5:
                shift = base_p - heavy_p
                if heavy_p < 0.1:
                    diagnosis = "REPRESENTATION DRIFT (score collapsed to near-zero)"
                elif heavy_p < 0.4:
                    diagnosis = "SCORE COMPRESSION (weakened but not collapsed)"
                else:
                    diagnosis = "THRESHOLD SHIFT (borderline, threshold-sensitive)"
                print(f"    {row['filename'][:40]:40s}: base={base_p:.4f}→heavy={heavy_p:.4f} "
                      f"shift={shift:+.4f} → {diagnosis}")

    # Check if augmented model fixes the regressions
    if "heavy+aug" in models_dict:
        fixed = sum(1 for row in shift_data
                    if row.get("heavy", 0) < 0.5 and row.get("heavy+aug", 0) >= 0.5)
        total_regressed = sum(1 for row in shift_data if row.get("heavy", 0) < 0.5)
        print(f"\n  Augmentation recovery: {fixed}/{total_regressed} regression clips fixed")

    return shift_data


def print_final_comparison(all_results, models_list):
    """Print final comparison table."""
    print("\n" + "=" * 70)
    print("STEP 5: FINAL COMPARISON — Does Heavy learn beyond shortcuts?")
    print("=" * 70)

    conditions = [
        ("original", "Original"),
        ("silence_standardized", "Silence-Std"),
        ("jakub_normalized", "Jakub OOD (norm)"),
        ("jakub_raw", "Jakub OOD (raw)"),
    ]

    header = f"{'Condition':25s}"
    for mname in models_list:
        header += f" {mname:>15s}"
    print(f"\n  {header}")
    print("  " + "-" * (25 + 16 * len(models_list)))

    for cond_key, cond_label in conditions:
        row = f"  {cond_label:25s}"
        for mname in models_list:
            if mname in all_results and cond_key in all_results[mname]:
                s = all_results[mname][cond_key]["summary"]
                row += f" {s['total_correct']}/{s['total']}={s['accuracy']:5.1%}"
            else:
                row += f" {'N/A':>15s}"
        print(row)

    # Amplitude robustness summary
    print(f"\n  {'Amplitude robustness':25s}")
    for gain in AMPLITUDE_GAINS:
        key = f"gain_{gain}"
        row = f"    gain={gain:.1f}x{'':17s}"
        for mname in models_list:
            if mname in all_results and "amplitude_variation" in all_results[mname]:
                s = all_results[mname]["amplitude_variation"].get(key, {})
                if s:
                    row += f" {s['total_correct']}/{s['total']}={s['accuracy']:5.1%}"
                else:
                    row += f" {'N/A':>15s}"
            else:
                row += f" {'N/A':>15s}"
        print(row)

    # Combined robustness
    print(f"\n  {'Combined (sil+amp)':25s}")
    for gain in [0.5, 1.0, 2.0, 2.5]:
        key = f"silgain_{gain}"
        row = f"    sil+gain={gain:.1f}x{'':13s}"
        for mname in models_list:
            if mname in all_results and "combined" in all_results[mname]:
                s = all_results[mname]["combined"].get(key, {})
                if s:
                    row += f" {s['total_correct']}/{s['total']}={s['accuracy']:5.1%}"
                else:
                    row += f" {'N/A':>15s}"
            else:
                row += f" {'N/A':>15s}"
        print(row)


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--skip-training", action="store_true",
                        help="Skip training, use existing augmented model")
    args = parser.parse_args()

    device = torch.device(args.device)
    print(f"Device: {device}")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    print(f"Dataset: {len(manifest['train'])} train, {len(manifest['val'])} val, "
          f"{len(manifest['held_out'])} held-out (locked)")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    # --- Step 1: Train augmented model ---
    aug_model_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy-augmented")

    if not args.skip_training:
        aug_model = train_heavy_with_augmentation(manifest, device, feature_extractor)
        del aug_model
        if device.type == "mps":
            torch.mps.empty_cache()
        elif device.type == "cuda":
            torch.cuda.empty_cache()

    # --- Load all models ---
    print("\nLoading models...")
    models = {}

    print("  Loading base...")
    models["base"] = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    models["base"].to(device)
    models["base"].eval()

    print("  Loading heavy...")
    models["heavy"] = AutoModelForAudioClassification.from_pretrained(
        os.path.join(MODELS_DIR, "wav2vec2-heavy"))
    models["heavy"].to(device)
    models["heavy"].eval()

    if os.path.exists(aug_model_dir):
        print("  Loading heavy+aug...")
        models["heavy+aug"] = AutoModelForAudioClassification.from_pretrained(aug_model_dir)
        models["heavy+aug"].to(device)
        models["heavy+aug"].eval()
    else:
        print("  WARNING: Augmented model not found, skipping")

    # --- Find Jakub clips ---
    jakub_clips = find_jakub_clips()
    print(f"\nFound {len(jakub_clips)} Jakub clips:")
    for c in jakub_clips:
        print(f"  {c['filename']} ({c['source']}, {c['language']}, {c['label_name']})")

    # --- Silence stats report ---
    print("\n" + "=" * 70)
    print("STEP 2: SILENCE STATISTICS")
    print("=" * 70)

    all_clips = manifest["train"] + manifest["val"]
    print(f"\n  {'Category':25s} {'Lead(ms)':>10s} {'Trail(ms)':>10s} {'VAD ratio':>10s}")
    print("  " + "-" * 58)

    for label_name in ["real", "fake"]:
        subset = [c for c in all_clips if c["label_name"] == label_name]
        leads, trails, vads = [], [], []
        for c in subset:
            audio, _ = librosa.load(c["path"], sr=SAMPLE_RATE, mono=True)
            stats = compute_silence_stats(audio)
            leads.append(stats["leading_silence_ms"])
            trails.append(stats["trailing_silence_ms"])
            vads.append(stats["vad_ratio"])
        print(f"  {label_name:25s} {np.mean(leads):10.1f} {np.mean(trails):10.1f} {np.mean(vads):10.4f}")

    print("\n  After standardization (20ms lead/trail):")
    for label_name in ["real", "fake"]:
        subset = [c for c in all_clips if c["label_name"] == label_name]
        leads, trails, vads = [], [], []
        for c in subset:
            audio, _ = librosa.load(c["path"], sr=SAMPLE_RATE, mono=True)
            audio_std = standardize_silence(audio)
            stats = compute_silence_stats(audio_std)
            leads.append(stats["leading_silence_ms"])
            trails.append(stats["trailing_silence_ms"])
            vads.append(stats["vad_ratio"])
        print(f"  {label_name:25s} {np.mean(leads):10.1f} {np.mean(trails):10.1f} {np.mean(vads):10.4f}")

    # --- Steps 2-4: Evaluation matrix ---
    all_results = run_evaluation_matrix(models, manifest, jakub_clips, device, feature_extractor)

    # --- Step 5: Final comparison ---
    models_list = list(models.keys())
    print_final_comparison(all_results, models_list)

    # --- Step 6: XTTS probability shift ---
    shift_data = run_xtts_probability_shift(models, jakub_clips, device, feature_extractor)

    # --- Save all results ---
    save_data = {
        "timestamp": timestamp,
        "device": str(device),
        "models": list(models.keys()),
    }
    # Serialize summaries (skip per_clip to keep file manageable)
    for mname in models:
        if mname in all_results:
            save_data[mname] = {}
            for cond, data in all_results[mname].items():
                if isinstance(data, dict) and "summary" in data:
                    save_data[mname][cond] = data["summary"]
                else:
                    save_data[mname][cond] = data
    save_data["xtts_shift"] = shift_data

    results_path = os.path.join(RESULTS_DIR, f"robustness_{timestamp}.json")
    with open(results_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nResults saved to {results_path}")

    # Cleanup
    for m in models.values():
        del m


if __name__ == "__main__":
    main()
