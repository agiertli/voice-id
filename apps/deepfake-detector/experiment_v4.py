"""
Experiment v4: Class-weight ablation with expanded dataset.

Dataset: All samples from voice-id/samples/ (66 AI + 31 natural, 3 speakers)
Augmentation: Amplitude only (same as successful heavy+aug)
Ablation:
  V4-A: new dataset + amplitude augmentation, NO class weights
  V4-B: new dataset + amplitude augmentation + class weights

Control: currently deployed heavy+aug model (immutable)

Evaluation:
  Primary: balanced accuracy
  Also: REAL acc, FAKE acc, FPR, FNR, ROC-AUC, ordinary accuracy
  p_fake distribution: min/median/mean/max for REAL and FAKE separately
  Speaker subgroup: Jakub/Jveverka (same person, in training — NOT OOD)

Usage:
    python experiment_v4.py --device cpu
    python experiment_v4.py --device cpu --skip-training
"""

import os
import json
import argparse
import warnings
import time
import subprocess
import tempfile
from collections import defaultdict

warnings.filterwarnings("ignore")

import numpy as np
import librosa
import torch
from torch import nn
from torch.utils.data import Dataset
from transformers import (
    AutoFeatureExtractor,
    AutoModelForAudioClassification,
    Trainer,
    TrainingArguments,
)
from sklearn.metrics import accuracy_score, balanced_accuracy_score, roc_auc_score

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(BASE_DIR, "..", "voice-id", "samples")
RESULTS_DIR = os.path.join(BASE_DIR, "results", "v4")
MODELS_DIR = os.path.join(BASE_DIR, "models")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"

SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10
TARGET_RMS = 0.05

AUDIO_EXTENSIONS = (".wav", ".flac", ".mp3", ".m4a", ".ogg")


# ============================================================
# Audio utilities
# ============================================================

def load_audio(path, sr=SAMPLE_RATE):
    """Load audio file, using ffmpeg conversion for formats soundfile can't handle."""
    try:
        audio, _ = librosa.load(path, sr=sr, mono=True)
        return audio
    except Exception:
        pass
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", str(sr), "-ac", "1", "-f", "wav", tmp.name],
            capture_output=True, check=True,
        )
        audio, _ = librosa.load(tmp.name, sr=sr, mono=True)
    return audio

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


def augment_amplitude(audio):
    gain = np.random.uniform(0.7, 1.8)
    return np.clip(audio * gain, -1.0, 1.0)


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
# Dataset scanning
# ============================================================

def scan_samples():
    """Scan voice-id/samples/ directory for all audio clips."""
    clips = []
    source_map = {"fish": "Fish Audio S2 Pro", "xtts": "Coqui XTTS v2"}

    # AI clips
    ai_dir = os.path.join(SAMPLES_DIR, "ai")
    for lang in ["cs", "en", "sk"]:
        lang_dir = os.path.join(ai_dir, lang)
        if not os.path.isdir(lang_dir):
            continue
        for f in sorted(os.listdir(lang_dir)):
            if not f.lower().endswith(AUDIO_EXTENSIONS):
                continue
            parts = f.rsplit(".", 1)[0].split("-")
            generator = parts[0] if parts else "unknown"
            speaker = parts[1] if len(parts) > 1 else "unknown"
            clips.append({
                "path": os.path.join(lang_dir, f),
                "filename": f,
                "label": 1,
                "label_name": "fake",
                "source": source_map.get(generator, generator),
                "generator": generator,
                "language": lang,
                "speaker": speaker,
            })

    # Natural clips
    natural_dir = os.path.join(SAMPLES_DIR, "natural")
    for lang in ["cs", "en", "sk"]:
        lang_dir = os.path.join(natural_dir, lang)
        if not os.path.isdir(lang_dir):
            continue
        for f in sorted(os.listdir(lang_dir)):
            if not f.lower().endswith(AUDIO_EXTENSIONS):
                continue
            parts = f.rsplit(".", 1)[0].split("-")
            speaker = parts[0] if parts else "unknown"
            clips.append({
                "path": os.path.join(lang_dir, f),
                "filename": f,
                "label": 0,
                "label_name": "real",
                "source": speaker,
                "generator": "human",
                "language": lang,
                "speaker": speaker,
            })

    return clips


def split_train_val(clips, val_ratio=0.2, seed=42):
    """Stratified train/val split. Jakub-named clips held out as same-speaker holdout.

    jakub and jveverka are the same person (Jakub Veverka).
    - jveverka clips (10 FLAC, natural/cs) go into train/val — model sees this speaker
    - jakub clips (1 natural en + 13 AI) held out — same-speaker holdout test
    This tests whether the model learns synthetic artifacts vs. speaker identity.
    """
    np.random.seed(seed)

    # Hold out clips filed under "jakub" (13 AI + 1 natural)
    # Keep "jveverka" clips (10 natural FLAC) in the training pool
    holdout = [c for c in clips if c["speaker"].lower() == "jakub"]
    pool = [c for c in clips if c["speaker"].lower() != "jakub"]

    # Stratify pool by (label, language)
    strata = defaultdict(list)
    for c in pool:
        key = (c["label"], c["language"])
        strata[key].append(c)

    train, val = [], []
    for key, group in strata.items():
        np.random.shuffle(group)
        n_val = max(1, int(len(group) * val_ratio))
        val.extend(group[:n_val])
        train.extend(group[n_val:])

    return train, val, holdout


# ============================================================
# Dataset class
# ============================================================

class V4AudioDataset(Dataset):
    def __init__(self, clips, feature_extractor, augment=False):
        self.clips = clips
        self.feature_extractor = feature_extractor
        self.augment = augment

    def __len__(self):
        return len(self.clips)

    def __getitem__(self, idx):
        clip = self.clips[idx]
        audio = load_audio(clip["path"])
        if self.augment and np.random.random() < 0.8:
            audio = augment_amplitude(audio)
        audio = preprocess_audio(audio)
        inputs = extract_features(audio, self.feature_extractor)
        item = {k: v.squeeze(0) for k, v in inputs.items()}
        item["labels"] = torch.tensor(clip["label"], dtype=torch.long)
        return item


# ============================================================
# Weighted loss trainer
# ============================================================

class WeightedTrainer(Trainer):
    def __init__(self, class_weights=None, **kwargs):
        super().__init__(**kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        logits = outputs.logits
        if self.class_weights is not None:
            weight = torch.tensor(self.class_weights, dtype=logits.dtype, device=logits.device)
            loss = nn.CrossEntropyLoss(weight=weight)(logits, labels)
        else:
            loss = nn.CrossEntropyLoss()(logits, labels)
        return (loss, outputs) if return_outputs else loss


# ============================================================
# Evaluation
# ============================================================

def evaluate_clips(model, feature_extractor, clips, device, audio_transform=None):
    model.eval()
    results = []
    for clip in clips:
        audio = load_audio(clip["path"])
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
            "source": clip.get("source", "unknown"),
            "generator": clip.get("generator", "unknown"),
            "language": clip.get("language", "?"),
            "speaker": clip.get("speaker", "?"),
            "prob_fake": float(prob_fake),
        })
    return results


def compute_full_metrics(results, threshold=0.5):
    """Compute comprehensive metrics per user spec."""
    if not results:
        return {}

    labels = np.array([r["label"] for r in results])
    probs = np.array([r["prob_fake"] for r in results])
    preds = (probs >= threshold).astype(int)

    real_mask = labels == 0
    fake_mask = labels == 1

    real_probs = probs[real_mask] if real_mask.any() else np.array([])
    fake_probs = probs[fake_mask] if fake_mask.any() else np.array([])

    real_correct = int(np.sum(preds[real_mask] == 0)) if real_mask.any() else 0
    real_total = int(real_mask.sum())
    fake_correct = int(np.sum(preds[fake_mask] == 1)) if fake_mask.any() else 0
    fake_total = int(fake_mask.sum())

    # FPR = false positive rate = real clips classified as fake / total real
    fpr = 1.0 - (real_correct / real_total) if real_total > 0 else 0.0
    # FNR = false negative rate = fake clips classified as real / total fake
    fnr = 1.0 - (fake_correct / fake_total) if fake_total > 0 else 0.0

    try:
        auc = roc_auc_score(labels, probs)
    except ValueError:
        auc = None

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
                "accuracy": v["correct"] / v["total"] if v["total"] else 0,
                "mean_pfake": float(np.mean(v["probs"]))}
            for k, v in sorted(groups.items())
        }

    def dist_stats(arr):
        if len(arr) == 0:
            return {"min": None, "median": None, "mean": None, "max": None}
        return {
            "min": float(np.min(arr)),
            "median": float(np.median(arr)),
            "mean": float(np.mean(arr)),
            "max": float(np.max(arr)),
        }

    return {
        "accuracy": float(accuracy_score(labels, preds)),
        "balanced_accuracy": float(balanced_accuracy_score(labels, preds)),
        "roc_auc": auc,
        "total_correct": int(np.sum(preds == labels)),
        "total": len(labels),
        "real_accuracy": real_correct / real_total if real_total > 0 else 0.0,
        "real_correct": real_correct,
        "real_total": real_total,
        "fake_accuracy": fake_correct / fake_total if fake_total > 0 else 0.0,
        "fake_correct": fake_correct,
        "fake_total": fake_total,
        "fpr": fpr,
        "fnr": fnr,
        "pfake_dist_real": dist_stats(real_probs),
        "pfake_dist_fake": dist_stats(fake_probs),
        "by_source": by_dim["source"],
        "by_language": by_dim["language"],
        "by_speaker": by_dim["speaker"],
    }


def print_full_metrics(label, metrics, per_clip=None, threshold=0.5):
    print(f"\n  [{label}]")
    print(f"  Ordinary accuracy:  {metrics['total_correct']}/{metrics['total']} = {metrics['accuracy']:.1%}")
    print(f"  Balanced accuracy:  {metrics['balanced_accuracy']:.1%}  ← PRIMARY METRIC")
    if metrics.get("roc_auc") is not None:
        print(f"  ROC-AUC:            {metrics['roc_auc']:.3f}")
    print(f"  REAL accuracy:      {metrics['real_correct']}/{metrics['real_total']} = {metrics['real_accuracy']:.1%}")
    print(f"  FAKE accuracy:      {metrics['fake_correct']}/{metrics['fake_total']} = {metrics['fake_accuracy']:.1%}")
    print(f"  FPR (real→fake):    {metrics['fpr']:.1%}")
    print(f"  FNR (fake→real):    {metrics['fnr']:.1%}")

    d_real = metrics["pfake_dist_real"]
    d_fake = metrics["pfake_dist_fake"]
    print(f"  p_fake distribution:")
    if d_real["min"] is not None:
        print(f"    REAL clips: min={d_real['min']:.4f}  median={d_real['median']:.4f}  "
              f"mean={d_real['mean']:.4f}  max={d_real['max']:.4f}")
    if d_fake["min"] is not None:
        print(f"    FAKE clips: min={d_fake['min']:.4f}  median={d_fake['median']:.4f}  "
              f"mean={d_fake['mean']:.4f}  max={d_fake['max']:.4f}")

    # Sanity check
    if metrics["real_accuracy"] == 0.0 and metrics["fake_accuracy"] == 1.0:
        print(f"  ⚠ SANITY CHECK FAILED: model predicts FAKE for every clip (balanced acc = 50%)")
    elif metrics["real_accuracy"] == 1.0 and metrics["fake_accuracy"] == 0.0:
        print(f"  ⚠ SANITY CHECK FAILED: model predicts REAL for every clip (balanced acc = 50%)")

    print(f"  By source:")
    for src, stats in sorted(metrics["by_source"].items()):
        print(f"    {src:25s}: {stats['correct']}/{stats['total']} = {stats['accuracy']:.0%} "
              f"(mean p_fake={stats['mean_pfake']:.4f})")
    print(f"  By language:")
    for lang, stats in sorted(metrics["by_language"].items()):
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
    bal = balanced_accuracy_score(labels, preds)
    acc = (preds == labels).mean()
    return {"accuracy": acc, "balanced_accuracy": bal}


def train_model(train_clips, val_clips, device, feature_extractor, model_name,
                use_class_weights=False):
    n_real = sum(1 for c in train_clips if c["label"] == 0)
    n_fake = sum(1 for c in train_clips if c["label"] == 1)
    n_total = len(train_clips)

    weight_label = "WITH class weights" if use_class_weights else "NO class weights"
    print(f"\n{'=' * 70}")
    print(f"TRAINING: {model_name} ({weight_label})")
    print(f"  Train: {n_total} clips ({n_real} real, {n_fake} fake, ratio {n_fake/max(n_real,1):.1f}:1)")
    print(f"  Val: {len(val_clips)} clips")

    class_weights = None
    if use_class_weights:
        w_real = n_total / (2.0 * n_real) if n_real > 0 else 1.0
        w_fake = n_total / (2.0 * n_fake) if n_fake > 0 else 1.0
        class_weights = [w_real, w_fake]
        print(f"  Class weights: real={w_real:.3f}, fake={w_fake:.3f}")

    print(f"{'=' * 70}")

    model = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    model.to(device)

    # Freeze CNN + bottom 12 transformers (same as Heavy config)
    for param in model.wav2vec2.feature_extractor.parameters():
        param.requires_grad = False
    for param in model.wav2vec2.feature_projection.parameters():
        param.requires_grad = False
    for i in range(12):
        for param in model.wav2vec2.encoder.layers[i].parameters():
            param.requires_grad = False

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_params = sum(p.numel() for p in model.parameters())
    print(f"  Trainable: {trainable:,} / {total_params:,} ({100 * trainable / total_params:.1f}%)")

    train_dataset = V4AudioDataset(train_clips, feature_extractor, augment=True)
    val_dataset = V4AudioDataset(val_clips, feature_extractor, augment=False)

    output_dir = os.path.join(MODELS_DIR, model_name)

    epochs = 15 if n_total <= 100 else 10

    training_args = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=epochs,
        per_device_train_batch_size=2,
        per_device_eval_batch_size=2,
        learning_rate=1e-5,
        weight_decay=0.01,
        warmup_steps=max(2, n_total // 10),
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="balanced_accuracy",
        greater_is_better=True,
        logging_steps=max(50, n_total // 2),
        fp16=False,
        dataloader_num_workers=0,
        use_cpu=(str(device) == "cpu"),
        report_to="none",
    )

    trainer_cls = WeightedTrainer if use_class_weights else Trainer
    trainer_kwargs = dict(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        compute_metrics=compute_metrics,
    )
    if use_class_weights:
        trainer_kwargs["class_weights"] = class_weights

    trainer = trainer_cls(**trainer_kwargs)

    print(f"  Training {epochs} epochs (best model by balanced_accuracy)...")
    trainer.train()

    model.save_pretrained(output_dir)
    feature_extractor.save_pretrained(output_dir)
    print(f"  Model saved to {output_dir}")

    return model


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--skip-training", action="store_true")
    args = parser.parse_args()

    device = torch.device(args.device)
    print(f"Device: {device}")

    # Scan all samples
    all_clips = scan_samples()
    print(f"\nScanned {len(all_clips)} total clips from {SAMPLES_DIR}")

    real_clips = [c for c in all_clips if c["label"] == 0]
    fake_clips = [c for c in all_clips if c["label"] == 1]
    print(f"  {len(real_clips)} real, {len(fake_clips)} fake")

    # Distribution report
    print(f"\n{'=' * 70}")
    print("DATASET DISTRIBUTION")
    print(f"{'=' * 70}")

    print("\n  Language x Class:")
    for lang in ["cs", "en", "sk"]:
        n_real = sum(1 for c in real_clips if c["language"] == lang)
        n_fake = sum(1 for c in fake_clips if c["language"] == lang)
        print(f"    {lang}: {n_real} real, {n_fake} fake")

    print("\n  Speaker distribution (real):")
    spk = defaultdict(lambda: defaultdict(int))
    for c in real_clips:
        spk[c["speaker"]][c["language"]] += 1
    for s, langs in sorted(spk.items()):
        lang_str = ", ".join(f"{l}={n}" for l, n in sorted(langs.items()))
        total = sum(langs.values())
        print(f"    {s}: {total} ({lang_str})")

    print("\n  Generator distribution (fake):")
    gen = defaultdict(lambda: defaultdict(int))
    for c in fake_clips:
        gen[c["source"]][c["language"]] += 1
    for g, langs in sorted(gen.items()):
        lang_str = ", ".join(f"{l}={n}" for l, n in sorted(langs.items()))
        total = sum(langs.values())
        print(f"    {g}: {total} ({lang_str})")

    # Split: jveverka (10 FLAC) in train/val, jakub (14 clips) as same-speaker holdout
    train_clips, val_clips, holdout_clips = split_train_val(all_clips)

    n_real_train = sum(1 for c in train_clips if c["label"] == 0)
    n_fake_train = sum(1 for c in train_clips if c["label"] == 1)
    n_real_val = sum(1 for c in val_clips if c["label"] == 0)
    n_fake_val = sum(1 for c in val_clips if c["label"] == 1)
    n_real_hold = sum(1 for c in holdout_clips if c["label"] == 0)
    n_fake_hold = sum(1 for c in holdout_clips if c["label"] == 1)

    print(f"\n  Train/val split:")
    print(f"    Train: {len(train_clips)} ({n_real_train} real, {n_fake_train} fake)")
    print(f"    Val:   {len(val_clips)} ({n_real_val} real, {n_fake_val} fake)")
    print(f"    Jveverka same-speaker holdout: {len(holdout_clips)} ({n_real_hold} real, {n_fake_hold} fake)")
    print(f"    NOTE: jakub==jveverka (same person). Model sees 10 jveverka natural clips in training.")
    print(f"          Holdout tests: does model detect AI clones of a voice it knows as natural?")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    # Train V4-A and V4-B
    if not args.skip_training:
        print("\n" + "#" * 70)
        print("# V4-A: Amplitude augmentation, NO class weights")
        print("#" * 70)
        model_a = train_model(train_clips, val_clips, device, feature_extractor,
                              "wav2vec2-v4a-no-weights", use_class_weights=False)
        del model_a

        print("\n" + "#" * 70)
        print("# V4-B: Amplitude augmentation + class weights")
        print("#" * 70)
        model_b = train_model(train_clips, val_clips, device, feature_extractor,
                              "wav2vec2-v4b-weighted", use_class_weights=True)
        del model_b

    # Load all models for evaluation
    print("\n\nLoading models for evaluation...")
    models = {}

    # Control: currently deployed heavy+aug
    aug_dir = os.path.join(MODELS_DIR, "wav2vec2-heavy-augmented")
    if os.path.exists(aug_dir):
        print("  Loading control: heavy+aug (deployed gary-finetuned)...")
        models["control:heavy+aug"] = AutoModelForAudioClassification.from_pretrained(aug_dir)
        models["control:heavy+aug"].to(device).eval()

    # V4-A
    v4a_dir = os.path.join(MODELS_DIR, "wav2vec2-v4a-no-weights")
    if os.path.exists(v4a_dir):
        print("  Loading V4-A (no class weights)...")
        models["V4-A"] = AutoModelForAudioClassification.from_pretrained(v4a_dir)
        models["V4-A"].to(device).eval()

    # V4-B
    v4b_dir = os.path.join(MODELS_DIR, "wav2vec2-v4b-weighted")
    if os.path.exists(v4b_dir):
        print("  Loading V4-B (class weights)...")
        models["V4-B"] = AutoModelForAudioClassification.from_pretrained(v4b_dir)
        models["V4-B"].to(device).eval()

    # Also load base (pretrained) for reference
    print("  Loading base (pretrained)...")
    models["base"] = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    models["base"].to(device).eval()

    # Evaluation clips = train + val (all non-OOD)
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
        print(f"\n{'━' * 70}")
        print(f"MODEL: {model_name}")
        print(f"{'━' * 70}")

        mr = {}

        # Original
        res = evaluate_clips(model, feature_extractor, eval_clips, device)
        mr["original"] = {"metrics": compute_full_metrics(res), "per_clip": res}
        print_full_metrics("Original", mr["original"]["metrics"])

        # Silence-standardized
        res_sil = evaluate_clips(model, feature_extractor, eval_clips, device,
                                  audio_transform=silence_std)
        mr["silence_std"] = {"metrics": compute_full_metrics(res_sil)}
        print_full_metrics("Silence-Std", mr["silence_std"]["metrics"])

        # Amplitude variation
        print("\n  Amplitude robustness:")
        amp = {}
        for gain in [0.5, 1.0, 2.0, 2.5]:
            res_a = evaluate_clips(model, feature_extractor, eval_clips, device,
                                    audio_transform=make_gain(gain))
            m = compute_full_metrics(res_a)
            print(f"    gain={gain:.1f}x: {m['total_correct']}/{m['total']}={m['accuracy']:.1%} "
                  f"(bal={m['balanced_accuracy']:.1%})")
            amp[f"gain_{gain}"] = m
        mr["amplitude"] = amp

        # Combined
        print("  Combined (sil+amp):")
        comb = {}
        for gain in [0.5, 1.0, 2.0, 2.5]:
            res_c = evaluate_clips(model, feature_extractor, eval_clips, device,
                                    audio_transform=combined(gain))
            m = compute_full_metrics(res_c)
            print(f"    sil+gain={gain:.1f}x: {m['total_correct']}/{m['total']}={m['accuracy']:.1%} "
                  f"(bal={m['balanced_accuracy']:.1%})")
            comb[f"sg_{gain}"] = m
        mr["combined"] = comb

        # Jveverka same-speaker holdout (model saw jveverka naturals in training)
        if holdout_clips:
            res_h_norm = evaluate_clips(model, feature_extractor, holdout_clips, device,
                                         audio_transform=norm_rms)
            mr["holdout_norm"] = {"metrics": compute_full_metrics(res_h_norm), "per_clip": res_h_norm}
            print_full_metrics("Jveverka same-speaker holdout (normalized)",
                               mr["holdout_norm"]["metrics"], per_clip=res_h_norm)

            res_h_raw = evaluate_clips(model, feature_extractor, holdout_clips, device)
            mr["holdout_raw"] = {"metrics": compute_full_metrics(res_h_raw), "per_clip": res_h_raw}
            print_full_metrics("Jveverka same-speaker holdout (raw)",
                               mr["holdout_raw"]["metrics"], per_clip=res_h_raw)

        all_results[model_name] = mr

    # ============================================================
    # Final comparison table
    # ============================================================

    model_names = list(models.keys())

    print(f"\n{'=' * 70}")
    print("FINAL COMPARISON: BALANCED ACCURACY (PRIMARY)")
    print(f"{'=' * 70}")

    conditions = [
        ("original", "Original"),
        ("silence_std", "Silence-Std"),
        ("holdout_norm", "Holdout (norm)"),
        ("holdout_raw", "Holdout (raw)"),
    ]

    header = f"\n  {'Condition':20s}"
    for mn in model_names:
        header += f" {mn:>18s}"
    print(header)
    print("  " + "-" * (20 + 19 * len(model_names)))

    for cond_key, cond_label in conditions:
        row = f"  {cond_label:20s}"
        for mn in model_names:
            if mn in all_results and cond_key in all_results[mn]:
                m = all_results[mn][cond_key]["metrics"]
                row += f"   {m['balanced_accuracy']:5.1%} ({m['accuracy']:5.1%})"
            else:
                row += f" {'N/A':>18s}"
        print(row)

    # Amplitude
    for gain in [0.5, 1.0, 2.0, 2.5]:
        key = f"gain_{gain}"
        row = f"  gain={gain:.1f}x{'':13s}"
        for mn in model_names:
            if mn in all_results and "amplitude" in all_results[mn]:
                m = all_results[mn]["amplitude"].get(key, {})
                if m:
                    row += f"   {m['balanced_accuracy']:5.1%} ({m['accuracy']:5.1%})"
                else:
                    row += f" {'N/A':>18s}"
            else:
                row += f" {'N/A':>18s}"
        print(row)

    # REAL vs FAKE accuracy
    print(f"\n{'=' * 70}")
    print("REAL vs FAKE ACCURACY (Original condition)")
    print(f"{'=' * 70}")

    header = f"\n  {'Metric':15s}"
    for mn in model_names:
        header += f" {mn:>18s}"
    print(header)
    print("  " + "-" * (15 + 19 * len(model_names)))

    for metric, label in [("real_accuracy", "REAL acc"), ("fake_accuracy", "FAKE acc"),
                           ("fpr", "FPR"), ("fnr", "FNR")]:
        row = f"  {label:15s}"
        for mn in model_names:
            if mn in all_results and "original" in all_results[mn]:
                m = all_results[mn]["original"]["metrics"]
                row += f"           {m[metric]:5.1%}"
            else:
                row += f" {'N/A':>18s}"
        print(row)

    # p_fake distributions
    print(f"\n{'=' * 70}")
    print("p_fake DISTRIBUTIONS (Original condition)")
    print(f"{'=' * 70}")

    for mn in model_names:
        if mn not in all_results or "original" not in all_results[mn]:
            continue
        m = all_results[mn]["original"]["metrics"]
        d_real = m["pfake_dist_real"]
        d_fake = m["pfake_dist_fake"]
        print(f"\n  {mn}:")
        if d_real["min"] is not None:
            print(f"    REAL: min={d_real['min']:.4f}  median={d_real['median']:.4f}  "
                  f"mean={d_real['mean']:.4f}  max={d_real['max']:.4f}")
        if d_fake["min"] is not None:
            print(f"    FAKE: min={d_fake['min']:.4f}  median={d_fake['median']:.4f}  "
                  f"mean={d_fake['mean']:.4f}  max={d_fake['max']:.4f}")
        # Separation check
        if d_real["max"] is not None and d_fake["min"] is not None:
            if d_real["max"] < d_fake["min"]:
                print(f"    ✓ Clean separation (real max < fake min)")
            elif d_real["median"] is not None and d_fake["median"] is not None:
                gap = d_fake["median"] - d_real["median"]
                print(f"    Median gap: {gap:+.4f} (>0 = fake scores higher)")

    # Save results
    save_data = {
        "timestamp": timestamp,
        "device": str(device),
        "models": model_names,
        "dataset": {
            "total": len(all_clips),
            "real": len(real_clips),
            "fake": len(fake_clips),
            "train": len(train_clips),
            "val": len(val_clips),
            "holdout": len(holdout_clips),
        },
    }
    for mn in model_names:
        if mn in all_results:
            save_data[mn] = {}
            for cond, data in all_results[mn].items():
                if isinstance(data, dict) and "metrics" in data:
                    save_data[mn][cond] = data["metrics"]
                else:
                    save_data[mn][cond] = data
    results_path = os.path.join(RESULTS_DIR, f"v4_{timestamp}.json")
    with open(results_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
