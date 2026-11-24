"""
Experiment v3: Fully robust detector training and evaluation.

Addresses all shortcut findings from v2:
  - Amplitude augmentation (fixes RMS shortcut)
  - Silence augmentation (fixes leading/trailing silence shortcut)
  - Recording-condition augmentation (noise, mild filtering)
  - Incorporates generated synthetic data if available

Models trained:
  - Heavy+Robust: Top 12 transformer layers + classifier, all augmentations

Evaluation matrix:
  - Original, silence-std, amplitude-varied, combined
  - Jakub OOD (normalized + raw)
  - Per-generator, per-language, per-speaker breakdown
  - XTTS probability shift diagnostic

Usage:
    python experiment_v3_robust.py --device cpu
    python experiment_v3_robust.py --device cpu --skip-training
    python experiment_v3_robust.py --device cpu --generated-data generated_data
"""

import os
import json
import argparse
import warnings
import time
from collections import defaultdict

warnings.filterwarnings("ignore")

import numpy as np
import librosa
import torch
from torch.utils.data import Dataset
from transformers import (
    AutoFeatureExtractor,
    AutoModelForAudioClassification,
    Trainer,
    TrainingArguments,
)
from sklearn.metrics import accuracy_score, balanced_accuracy_score, roc_auc_score

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(BASE_DIR, "experiment_manifest.json")
RESULTS_DIR = os.path.join(BASE_DIR, "results", "v3")
MODELS_DIR = os.path.join(BASE_DIR, "models")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"

SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10
TARGET_RMS = 0.05


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


def extract_features(audio, feature_extractor):
    return feature_extractor(
        audio, sampling_rate=SAMPLE_RATE, return_tensors="pt",
        padding="max_length", max_length=MAX_LENGTH, truncation=True,
    )


# ============================================================
# Augmentations
# ============================================================

def augment_amplitude(audio):
    """Random gain between 0.7x and 1.8x."""
    gain = np.random.uniform(0.7, 1.8)
    return np.clip(audio * gain, -1.0, 1.0)


def augment_silence(audio, sr=SAMPLE_RATE):
    """Randomize leading/trailing silence: trim then add random 0-80ms each."""
    trimmed, _ = librosa.effects.trim(audio, top_db=30)
    lead_ms = np.random.uniform(0, 80)
    trail_ms = np.random.uniform(0, 80)
    lead_samples = int(lead_ms / 1000 * sr)
    trail_samples = int(trail_ms / 1000 * sr)
    return np.concatenate([
        np.zeros(lead_samples, dtype=np.float32),
        trimmed,
        np.zeros(trail_samples, dtype=np.float32),
    ])


def augment_noise(audio, snr_range=(20, 40)):
    """Add mild Gaussian noise at random SNR."""
    snr_db = np.random.uniform(*snr_range)
    signal_power = np.mean(audio ** 2)
    noise_power = signal_power / (10 ** (snr_db / 10))
    noise = np.random.normal(0, np.sqrt(noise_power), len(audio)).astype(np.float32)
    return np.clip(audio + noise, -1.0, 1.0)


def augment_lowpass(audio, sr=SAMPLE_RATE):
    """Mild low-pass filter simulating phone/mic frequency response."""
    cutoff_hz = np.random.uniform(3000, 7000)
    from scipy.signal import butter, lfilter
    nyquist = sr / 2
    normalized_cutoff = cutoff_hz / nyquist
    if normalized_cutoff >= 1.0:
        return audio
    b, a = butter(2, normalized_cutoff, btype='low')
    filtered = lfilter(b, a, audio).astype(np.float32)
    return np.clip(filtered, -1.0, 1.0)


def apply_augmentations(audio, sr=SAMPLE_RATE):
    """Apply augmentations with calibrated probability."""
    # 80% chance of silence augmentation
    if np.random.random() < 0.8:
        audio = augment_silence(audio, sr)

    # 80% chance of amplitude augmentation
    if np.random.random() < 0.8:
        audio = augment_amplitude(audio)

    # 30% chance of noise
    if np.random.random() < 0.3:
        audio = augment_noise(audio)

    # 20% chance of low-pass filter
    if np.random.random() < 0.2:
        audio = augment_lowpass(audio, sr)

    return audio


# ============================================================
# Dataset
# ============================================================

class RobustAudioDataset(Dataset):
    def __init__(self, clips, feature_extractor, augment=False):
        self.clips = clips
        self.feature_extractor = feature_extractor
        self.augment = augment

    def __len__(self):
        return len(self.clips)

    def __getitem__(self, idx):
        clip = self.clips[idx]
        audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)

        if self.augment:
            audio = apply_augmentations(audio)

        audio = preprocess_audio(audio)
        inputs = extract_features(audio, self.feature_extractor)
        item = {k: v.squeeze(0) for k, v in inputs.items()}
        item["labels"] = torch.tensor(clip["label"], dtype=torch.long)
        return item


# ============================================================
# Evaluation
# ============================================================

def evaluate_clips(model, feature_extractor, clips, device, audio_transform=None):
    model.eval()
    results = []
    for clip in clips:
        audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)
        if audio_transform:
            audio = audio_transform(audio)
        audio = preprocess_audio(audio)
        inputs = extract_features(audio, feature_extractor)
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)
            prob_fake = probs[0][1].item()
        results.append({
            "filename": clip.get("filename", os.path.basename(clip["path"])),
            "label": clip["label"],
            "label_name": clip.get("label_name", "fake" if clip["label"] == 1 else "real"),
            "source": clip.get("source", clip.get("generator_full", "unknown")),
            "language": clip.get("language", "?"),
            "speaker": clip.get("speaker", "?"),
            "prob_fake": float(prob_fake),
        })
    return results


def summarize_results(results, threshold=0.5):
    if not results:
        return {"accuracy": 0, "total": 0}
    labels = np.array([r["label"] for r in results])
    probs = np.array([r["prob_fake"] for r in results])
    preds = (probs >= threshold).astype(int)

    real_mask = labels == 0
    fake_mask = labels == 1

    by_dim = {}
    for dim in ["source", "language", "speaker"]:
        groups = defaultdict(lambda: {"correct": 0, "total": 0, "probs": []})
        for r in results:
            pred = 1 if r["prob_fake"] >= threshold else 0
            key = r.get(dim, "?")
            groups[key]["total"] += 1
            if pred == r["label"]:
                groups[key]["correct"] += 1
            groups[key]["probs"].append(r["prob_fake"])
        by_dim[dim] = {
            k: {"correct": v["correct"], "total": v["total"],
                "accuracy": v["correct"]/v["total"] if v["total"] else 0,
                "mean_pfake": float(np.mean(v["probs"]))}
            for k, v in sorted(groups.items())
        }

    try:
        auc = roc_auc_score(labels, probs)
    except ValueError:
        auc = None

    return {
        "accuracy": float(accuracy_score(labels, preds)),
        "balanced_accuracy": float(balanced_accuracy_score(labels, preds)),
        "roc_auc": auc,
        "total_correct": int(np.sum(preds == labels)),
        "total": len(labels),
        "real_correct": int(np.sum(preds[real_mask] == 0)) if real_mask.any() else 0,
        "real_total": int(real_mask.sum()),
        "fake_correct": int(np.sum(preds[fake_mask] == 1)) if fake_mask.any() else 0,
        "fake_total": int(fake_mask.sum()),
        "mean_pfake_real": float(np.mean(probs[real_mask])) if real_mask.any() else None,
        "mean_pfake_fake": float(np.mean(probs[fake_mask])) if fake_mask.any() else None,
        "by_source": by_dim["source"],
        "by_language": by_dim["language"],
        "by_speaker": by_dim["speaker"],
    }


def print_summary(label, summary, per_clip=None, threshold=0.5):
    print(f"\n  [{label}]")
    print(f"  Accuracy: {summary['total_correct']}/{summary['total']} = {summary['accuracy']:.1%}")
    if summary.get("balanced_accuracy"):
        print(f"  Balanced: {summary['balanced_accuracy']:.1%}")
    if summary.get("roc_auc") is not None:
        print(f"  ROC-AUC: {summary['roc_auc']:.3f}")
    if summary['real_total'] > 0:
        print(f"  REAL: {summary['real_correct']}/{summary['real_total']} "
              f"(mean p_fake={summary['mean_pfake_real']:.4f})")
    if summary['fake_total'] > 0:
        print(f"  FAKE: {summary['fake_correct']}/{summary['fake_total']} "
              f"(mean p_fake={summary['mean_pfake_fake']:.4f})")
    print(f"  By source:")
    for src, stats in sorted(summary["by_source"].items()):
        print(f"    {src:25s}: {stats['correct']}/{stats['total']} = {stats['accuracy']:.0%} "
              f"(mean p_fake={stats['mean_pfake']:.4f})")
    print(f"  By language:")
    for lang, stats in sorted(summary["by_language"].items()):
        print(f"    {lang:25s}: {stats['correct']}/{stats['total']} = {stats['accuracy']:.0%}")
    if per_clip:
        for r in per_clip:
            pred = "FAKE" if r["prob_fake"] >= threshold else "REAL"
            actual = "FAKE" if r["label"] == 1 else "REAL"
            mark = "+" if pred == actual else "X"
            print(f"    {mark} {r['filename'][:45]:45s} {actual:4s}→{pred:4s} p_fake={r['prob_fake']:.4f}")


# ============================================================
# Training
# ============================================================

def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {"accuracy": (preds == labels).mean()}


def train_robust_model(train_clips, val_clips, device, feature_extractor):
    print("\n" + "=" * 70)
    print("TRAINING: Heavy + All Augmentations (amplitude + silence + noise + lowpass)")
    print(f"  Train clips: {len(train_clips)}")
    print(f"  Val clips: {len(val_clips)}")
    print("=" * 70)

    model = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    model.to(device)

    # Freeze CNN + bottom 12 transformers (same as original Heavy)
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

    train_dataset = RobustAudioDataset(train_clips, feature_extractor, augment=True)
    val_dataset = RobustAudioDataset(val_clips, feature_extractor, augment=False)

    output_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy-robust")

    # Scale epochs based on dataset size
    n_train = len(train_clips)
    if n_train <= 40:
        epochs = 20
    elif n_train <= 100:
        epochs = 15
    else:
        epochs = 10

    training_args = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=epochs,
        per_device_train_batch_size=2,
        per_device_eval_batch_size=2,
        learning_rate=1e-5,
        weight_decay=0.01,
        warmup_steps=max(2, n_train // 10),
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        logging_steps=max(50, n_train // 2),
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

    print(f"  Training {epochs} epochs...")
    trainer.train()

    model.save_pretrained(output_dir)
    feature_extractor.save_pretrained(output_dir)
    print(f"  Model saved to {output_dir}")

    return model


# ============================================================
# Jakub clips finder
# ============================================================

def find_jakub_clips():
    clips = []
    samples_base = os.path.join(BASE_DIR, "..", "voice-id", "samples")
    source_map = {"fish": "Fish Audio S2 Pro", "xtts": "Coqui XTTS v2"}

    for subdir in ["ai/cs", "ai/en", "ai/sk"]:
        search_dir = os.path.join(samples_base, subdir)
        if not os.path.isdir(search_dir):
            continue
        for f in sorted(os.listdir(search_dir)):
            if "jakub" in f.lower() and f.endswith((".wav", ".mp3", ".m4a")):
                parts = f.rsplit(".", 1)[0].split("-")
                source = parts[0] if parts else "unknown"
                lang = subdir.split("/")[-1]
                clips.append({
                    "path": os.path.join(search_dir, f),
                    "filename": f,
                    "label": 1, "label_name": "fake",
                    "source": source_map.get(source, source),
                    "language": lang, "speaker": "jakub",
                })

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
                    "label": 0, "label_name": "real",
                    "source": "jakub", "language": lang, "speaker": "jakub",
                })
    return clips


def standardize_silence(audio, sr=SAMPLE_RATE, lead_ms=20, trail_ms=20):
    trimmed, _ = librosa.effects.trim(audio, top_db=30)
    lead_samples = int(lead_ms / 1000 * sr)
    trail_samples = int(trail_ms / 1000 * sr)
    return np.concatenate([
        np.zeros(lead_samples, dtype=np.float32),
        trimmed,
        np.zeros(trail_samples, dtype=np.float32),
    ])


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--skip-training", action="store_true")
    parser.add_argument("--generated-data", default=None,
                        help="Path to generated_data dir with generation_manifest.json")
    args = parser.parse_args()

    device = torch.device(args.device)
    print(f"Device: {device}")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    train_clips = list(manifest["train"])
    val_clips = list(manifest["val"])

    # Incorporate generated data if available
    gen_clips = []
    if args.generated_data:
        gen_manifest_path = os.path.join(args.generated_data, "generation_manifest.json")
        if os.path.exists(gen_manifest_path):
            with open(gen_manifest_path) as f:
                gen_data = json.load(f)

            valid_gen = [c for c in gen_data["clips"] if os.path.exists(c["path"])]
            print(f"Generated data: {len(valid_gen)} valid clips from {gen_manifest_path}")

            # Split generated data: 80% train, 20% val (stratified by generator)
            np.random.seed(42)
            xtts_gen = [c for c in valid_gen if c["generator"] == "xtts"]
            fish_gen = [c for c in valid_gen if c["generator"] == "fish"]

            for subset in [xtts_gen, fish_gen]:
                np.random.shuffle(subset)
                split = int(len(subset) * 0.8)
                train_clips.extend(subset[:split])
                val_clips.extend(subset[split:])
                gen_clips.extend(subset)

    print(f"Final dataset: {len(train_clips)} train, {len(val_clips)} val")
    real_train = sum(1 for c in train_clips if c["label"] == 0)
    fake_train = sum(1 for c in train_clips if c["label"] == 1)
    print(f"  Train: {real_train} real, {fake_train} fake")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    # Train
    robust_model_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy-robust")
    if not args.skip_training:
        robust_model = train_robust_model(train_clips, val_clips, device, feature_extractor)
        del robust_model

    # Load all models
    print("\nLoading models...")
    models = {}

    print("  Loading base (pretrained)...")
    models["base"] = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    models["base"].to(device).eval()

    heavy_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy")
    if os.path.exists(heavy_dir):
        print("  Loading heavy (original)...")
        models["heavy"] = AutoModelForAudioClassification.from_pretrained(heavy_dir)
        models["heavy"].to(device).eval()

    aug_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy-augmented")
    if os.path.exists(aug_dir):
        print("  Loading heavy+aug (amplitude only)...")
        models["heavy+aug"] = AutoModelForAudioClassification.from_pretrained(aug_dir)
        models["heavy+aug"].to(device).eval()

    if os.path.exists(robust_model_dir):
        print("  Loading heavy+robust (all augmentations)...")
        models["heavy+robust"] = AutoModelForAudioClassification.from_pretrained(robust_model_dir)
        models["heavy+robust"].to(device).eval()

    # Jakub OOD
    jakub_clips = find_jakub_clips()
    print(f"\nJakub OOD: {len(jakub_clips)} clips")

    # Evaluation clips (train + val, NOT locked test)
    eval_clips = train_clips + val_clips

    # Audio transforms
    def silence_std(audio):
        return standardize_silence(audio)

    def make_gain(g):
        return lambda audio: np.clip(audio * g, -1.0, 1.0)

    def combined(g):
        def transform(audio):
            audio = standardize_silence(audio)
            return np.clip(audio * g, -1.0, 1.0)
        return transform

    def norm_rms(audio):
        return normalize_rms(audio)

    all_results = {}

    for model_name, model in models.items():
        print(f"\n{'━' * 60}")
        print(f"MODEL: {model_name}")
        print(f"{'━' * 60}")

        mr = {}

        # Original
        res = evaluate_clips(model, feature_extractor, eval_clips, device)
        mr["original"] = {"summary": summarize_results(res), "per_clip": res}
        print_summary("Original", mr["original"]["summary"])

        # Silence-standardized
        res_sil = evaluate_clips(model, feature_extractor, eval_clips, device,
                                  audio_transform=silence_std)
        mr["silence_std"] = {"summary": summarize_results(res_sil)}
        print_summary("Silence-Std", mr["silence_std"]["summary"])

        # Amplitude variation
        print("\n  Amplitude robustness:")
        amp = {}
        for gain in [0.5, 1.0, 2.0, 2.5]:
            res_a = evaluate_clips(model, feature_extractor, eval_clips, device,
                                    audio_transform=make_gain(gain))
            s = summarize_results(res_a)
            print(f"    gain={gain:.1f}x: {s['total_correct']}/{s['total']}={s['accuracy']:.1%}")
            amp[f"gain_{gain}"] = s
        mr["amplitude"] = amp

        # Combined
        print("  Combined (sil+amp):")
        comb = {}
        for gain in [0.5, 1.0, 2.0, 2.5]:
            res_c = evaluate_clips(model, feature_extractor, eval_clips, device,
                                    audio_transform=combined(gain))
            s = summarize_results(res_c)
            print(f"    sil+gain={gain:.1f}x: {s['total_correct']}/{s['total']}={s['accuracy']:.1%}")
            comb[f"sg_{gain}"] = s
        mr["combined"] = comb

        # Jakub OOD
        if jakub_clips:
            res_j_norm = evaluate_clips(model, feature_extractor, jakub_clips, device,
                                         audio_transform=norm_rms)
            mr["jakub_norm"] = {"summary": summarize_results(res_j_norm), "per_clip": res_j_norm}
            print_summary("Jakub OOD (normalized)", mr["jakub_norm"]["summary"], per_clip=res_j_norm)

            res_j_raw = evaluate_clips(model, feature_extractor, jakub_clips, device)
            mr["jakub_raw"] = {"summary": summarize_results(res_j_raw), "per_clip": res_j_raw}
            print_summary("Jakub OOD (raw)", mr["jakub_raw"]["summary"], per_clip=res_j_raw)

        all_results[model_name] = mr

    # Final comparison table
    print("\n" + "=" * 70)
    print("FINAL COMPARISON TABLE")
    print("=" * 70)

    model_names = list(models.keys())
    conditions = [
        ("original", "Original"),
        ("silence_std", "Silence-Std"),
        ("jakub_norm", "Jakub OOD (norm)"),
        ("jakub_raw", "Jakub OOD (raw)"),
    ]

    header = f"  {'Condition':25s}"
    for mn in model_names:
        header += f" {mn:>15s}"
    print(f"\n{header}")
    print("  " + "-" * (25 + 16 * len(model_names)))

    for cond_key, cond_label in conditions:
        row = f"  {cond_label:25s}"
        for mn in model_names:
            if mn in all_results and cond_key in all_results[mn]:
                s = all_results[mn][cond_key]["summary"]
                row += f" {s['total_correct']}/{s['total']}={s['accuracy']:5.1%}"
            else:
                row += f" {'N/A':>15s}"
        print(row)

    # Amplitude robustness
    for gain in [0.5, 1.0, 2.0, 2.5]:
        key = f"gain_{gain}"
        row = f"  gain={gain:.1f}x{'':18s}"
        for mn in model_names:
            if mn in all_results and "amplitude" in all_results[mn]:
                s = all_results[mn]["amplitude"].get(key, {})
                if s:
                    row += f" {s['total_correct']}/{s['total']}={s['accuracy']:5.1%}"
                else:
                    row += f" {'N/A':>15s}"
            else:
                row += f" {'N/A':>15s}"
        print(row)

    # Combined
    for gain in [0.5, 1.0, 2.0, 2.5]:
        key = f"sg_{gain}"
        row = f"  sil+gain={gain:.1f}x{'':14s}"
        for mn in model_names:
            if mn in all_results and "combined" in all_results[mn]:
                s = all_results[mn]["combined"].get(key, {})
                if s:
                    row += f" {s['total_correct']}/{s['total']}={s['accuracy']:5.1%}"
                else:
                    row += f" {'N/A':>15s}"
            else:
                row += f" {'N/A':>15s}"
        print(row)

    # XTTS probability shift
    print("\n" + "=" * 70)
    print("XTTS PROBABILITY SHIFT DIAGNOSTIC")
    print("=" * 70)

    xtts_clips = [c for c in jakub_clips if "XTTS" in c.get("source", "")]
    if xtts_clips:
        print(f"\n  {'Clip':45s}", end="")
        for mn in model_names:
            print(f" {mn:>12s}", end="")
        print()
        print("  " + "-" * (45 + 13 * len(model_names)))

        for clip in xtts_clips:
            print(f"  {clip['filename'][:45]:45s}", end="")
            for mn, model in models.items():
                res = evaluate_clips(model, feature_extractor, [clip], device,
                                      audio_transform=norm_rms)
                pf = res[0]["prob_fake"]
                mark = "+" if pf >= 0.5 else "X"
                print(f" {pf:>7.4f} ({mark})", end="")
            print()

        # Recovery stats
        if "heavy" in models and "heavy+robust" in models:
            regressed = 0
            fixed = 0
            for clip in xtts_clips:
                h_res = evaluate_clips(models["heavy"], feature_extractor, [clip], device,
                                        audio_transform=norm_rms)
                r_res = evaluate_clips(models["heavy+robust"], feature_extractor, [clip], device,
                                        audio_transform=norm_rms)
                if h_res[0]["prob_fake"] < 0.5:
                    regressed += 1
                    if r_res[0]["prob_fake"] >= 0.5:
                        fixed += 1
            print(f"\n  Robust model recovery: {fixed}/{regressed} regression clips fixed")

    # Per-language breakdown for robust model
    if "heavy+robust" in all_results:
        print("\n" + "=" * 70)
        print("PER-LANGUAGE BREAKDOWN (heavy+robust)")
        print("=" * 70)
        s = all_results["heavy+robust"]["original"]["summary"]
        print(f"\n  {'Language':10s} {'Correct':>8s} {'Total':>6s} {'Acc':>6s}")
        for lang, stats in sorted(s.get("by_language", {}).items()):
            print(f"  {lang:10s} {stats['correct']:8d} {stats['total']:6d} {stats['accuracy']:6.0%}")

    # Save results
    save_data = {
        "timestamp": timestamp,
        "device": str(device),
        "models": model_names,
        "train_size": len(train_clips),
        "val_size": len(val_clips),
        "generated_clips": len(gen_clips),
    }
    for mn in model_names:
        if mn in all_results:
            save_data[mn] = {}
            for cond, data in all_results[mn].items():
                if isinstance(data, dict) and "summary" in data:
                    save_data[mn][cond] = data["summary"]
                else:
                    save_data[mn][cond] = data
    results_path = os.path.join(RESULTS_DIR, f"robust_{timestamp}.json")
    with open(results_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
