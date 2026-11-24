"""
Experiment V4.1: Data-diversity expansion.

Question: Can the detector learn to recognize XTTS-the-engine rather than
specific XTTS voices?

Design:
  - V4-A frozen as immutable control
  - V4.1 trained with V4-A recipe (no class weights, amplitude augmentation)
  - Expanded dataset: original V4 + diverse-voice naturals + XTTS clones
  - Voice-disjoint split: no speaker crosses train/test boundary

Evaluation hierarchy:
  1. In-distribution validation (val split from train pool)
  2. Same-speaker holdout (jakub — model saw jveverka naturals)
  3. Unseen synthetic voice (XTTS clones of test speakers)
  4. Unseen human speaker (naturals of test speakers)

Train speakers: anton, miska, jveverka + delta, echo, hotel, india, kilo, lima
Test speakers: foxtrot, golf, juliet, mike (NEVER in training)
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
from torch import nn
from torch.utils.data import Dataset
from transformers import (
    AutoFeatureExtractor,
    AutoModelForAudioClassification,
    Trainer,
    TrainingArguments,
)
from sklearn.metrics import accuracy_score, balanced_accuracy_score, roc_auc_score
import subprocess, tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(BASE_DIR, "..", "voice-id", "samples")
DIVERSE_DIR = os.path.join(BASE_DIR, "data", "diverse-voices")
CLONES_DIR = os.path.join(BASE_DIR, "data", "xtts-clones")
RESULTS_DIR = os.path.join(BASE_DIR, "results", "v41")
MODELS_DIR = os.path.join(BASE_DIR, "models")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"

SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10
TARGET_RMS = 0.05

AUDIO_EXTENSIONS = (".wav", ".flac", ".mp3", ".m4a", ".ogg")

TRAIN_SPEAKERS = {"delta", "echo", "hotel", "india", "kilo", "lima"}
TEST_SPEAKERS = {"foxtrot", "golf", "juliet", "mike"}


def load_audio(path, sr=SAMPLE_RATE):
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


def normalize_rms(audio, target_rms=TARGET_RMS):
    current_rms = np.sqrt(np.mean(audio ** 2))
    if current_rms > 1e-6:
        audio = audio * (target_rms / current_rms)
    return np.clip(audio, -1.0, 1.0)


def scan_original_samples():
    """Scan voice-id/samples/ — same as V4."""
    clips = []
    source_map = {"fish": "Fish Audio S2 Pro", "xtts": "Coqui XTTS v2"}

    ai_dir = os.path.join(SAMPLES_DIR, "ai")
    for lang in ["cs", "en", "sk"]:
        lang_dir = os.path.join(ai_dir, lang)
        if not os.path.isdir(lang_dir):
            continue
        for f in sorted(os.listdir(lang_dir)):
            if not f.lower().endswith(AUDIO_EXTENSIONS):
                continue
            if "-old" in f:
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
                "origin": "v4-original",
            })

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
                "origin": "v4-original",
            })

    return clips


def scan_diverse_voices():
    """Scan diverse-voices naturals."""
    clips = []
    with open(os.path.join(DIVERSE_DIR, "manifest.json")) as f:
        manifest = json.load(f)

    for speaker, info in manifest.items():
        lang = info["source_language"]
        for clip_info in info["clips"]:
            path = os.path.join(DIVERSE_DIR, speaker, clip_info["file"])
            if not os.path.exists(path):
                print(f"  WARNING: missing {path}")
                continue
            clips.append({
                "path": path,
                "filename": clip_info["file"],
                "label": 0,
                "label_name": "real",
                "source": speaker,
                "generator": "human",
                "language": lang,
                "speaker": speaker,
                "origin": "diverse-natural",
            })
    return clips


def scan_xtts_clones():
    """Scan XTTS clone_speaker outputs."""
    clips = []
    with open(os.path.join(CLONES_DIR, "clones.json")) as f:
        clones = json.load(f)

    for split_name in ["train", "test"]:
        for entry in clones.get(split_name, []):
            path = os.path.join(CLONES_DIR, entry["file"])
            if not os.path.exists(path):
                print(f"  WARNING: missing {path}")
                continue
            clips.append({
                "path": path,
                "filename": entry["file"],
                "label": 1,
                "label_name": "fake",
                "source": "XTTS clone",
                "generator": "xtts-clone",
                "language": entry["language"],
                "speaker": entry["speaker"],
                "origin": "diverse-xtts-clone",
                "clone_split": split_name,
            })
    return clips


def build_splits(original_clips, diverse_naturals, xtts_clones):
    """Build voice-disjoint train/val/test splits.

    Train pool: original V4 (except jakub holdout) + train-speaker naturals + train-speaker clones
    Jakub holdout: same as V4 (14 clips, same-speaker test)
    Test unseen synthetic: XTTS clones of test speakers
    Test unseen natural: naturals of test speakers
    """
    np.random.seed(42)

    jakub_holdout = [c for c in original_clips if c["speaker"].lower() == "jakub"]
    original_pool = [c for c in original_clips if c["speaker"].lower() != "jakub"]

    train_diverse_natural = [c for c in diverse_naturals if c["speaker"] in TRAIN_SPEAKERS]
    test_diverse_natural = [c for c in diverse_naturals if c["speaker"] in TEST_SPEAKERS]

    train_clones = [c for c in xtts_clones if c["speaker"] in TRAIN_SPEAKERS]
    test_clones = [c for c in xtts_clones if c["speaker"] in TEST_SPEAKERS]

    train_pool = original_pool + train_diverse_natural + train_clones

    strata = defaultdict(list)
    for c in train_pool:
        key = (c["label"], c["language"])
        strata[key].append(c)

    train, val = [], []
    for key, group in strata.items():
        np.random.shuffle(group)
        n_val = max(1, int(len(group) * 0.2))
        val.extend(group[:n_val])
        train.extend(group[n_val:])

    return {
        "train": train,
        "val": val,
        "jakub_holdout": jakub_holdout,
        "test_unseen_synthetic": test_clones,
        "test_unseen_natural": test_diverse_natural,
    }


class AudioDataset(Dataset):
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


def evaluate_clips(model, feature_extractor, clips, device):
    model.eval()
    results = []
    for clip in clips:
        audio = load_audio(clip["path"])
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
            "origin": clip.get("origin", "?"),
            "prob_fake": float(prob_fake),
        })
    return results


def compute_metrics_dict(results, threshold=0.5):
    if not results:
        return {"accuracy": 0, "balanced_accuracy": 0, "total": 0}

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

    fpr = 1.0 - (real_correct / real_total) if real_total > 0 else 0.0
    fnr = 1.0 - (fake_correct / fake_total) if fake_total > 0 else 0.0

    try:
        auc = roc_auc_score(labels, probs)
    except ValueError:
        auc = None

    def dist_stats(arr):
        if len(arr) == 0:
            return {"min": None, "median": None, "mean": None, "max": None}
        return {"min": float(np.min(arr)), "median": float(np.median(arr)),
                "mean": float(np.mean(arr)), "max": float(np.max(arr))}

    by_dim = {}
    for dim in ["source", "language", "speaker", "generator"]:
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

    return {
        "accuracy": float(accuracy_score(labels, preds)),
        "balanced_accuracy": float(balanced_accuracy_score(labels, preds)),
        "roc_auc": auc,
        "total_correct": int(np.sum(preds == labels)),
        "total": len(labels),
        "real_accuracy": real_correct / real_total if real_total > 0 else 0.0,
        "real_correct": real_correct, "real_total": real_total,
        "fake_accuracy": fake_correct / fake_total if fake_total > 0 else 0.0,
        "fake_correct": fake_correct, "fake_total": fake_total,
        "fpr": fpr, "fnr": fnr,
        "pfake_dist_real": dist_stats(real_probs),
        "pfake_dist_fake": dist_stats(fake_probs),
        "by_source": by_dim["source"],
        "by_language": by_dim["language"],
        "by_speaker": by_dim["speaker"],
        "by_generator": by_dim["generator"],
    }


def print_metrics(label, metrics, per_clip=None, threshold=0.5):
    print(f"\n  [{label}]")
    print(f"  Accuracy:           {metrics.get('total_correct',0)}/{metrics.get('total',0)} = {metrics.get('accuracy',0):.1%}")
    print(f"  Balanced accuracy:  {metrics.get('balanced_accuracy',0):.1%}  ← PRIMARY")
    if metrics.get("roc_auc") is not None:
        print(f"  ROC-AUC:            {metrics['roc_auc']:.3f}")
    print(f"  REAL acc:           {metrics.get('real_correct',0)}/{metrics.get('real_total',0)} = {metrics.get('real_accuracy',0):.1%}")
    print(f"  FAKE acc:           {metrics.get('fake_correct',0)}/{metrics.get('fake_total',0)} = {metrics.get('fake_accuracy',0):.1%}")
    print(f"  FPR (real→fake):    {metrics.get('fpr',0):.1%}")
    print(f"  FNR (fake→real):    {metrics.get('fnr',0):.1%}")

    d_r = metrics.get("pfake_dist_real", {})
    d_f = metrics.get("pfake_dist_fake", {})
    if d_r.get("min") is not None:
        print(f"  p_fake REAL: min={d_r['min']:.4f} med={d_r['median']:.4f} mean={d_r['mean']:.4f} max={d_r['max']:.4f}")
    if d_f.get("min") is not None:
        print(f"  p_fake FAKE: min={d_f['min']:.4f} med={d_f['median']:.4f} mean={d_f['mean']:.4f} max={d_f['max']:.4f}")

    if metrics.get("by_source"):
        print(f"  By source:")
        for src, s in sorted(metrics["by_source"].items()):
            print(f"    {src:25s}: {s['correct']}/{s['total']}={s['accuracy']:.0%} (mean p_fake={s['mean_pfake']:.4f})")

    if per_clip:
        for r in per_clip:
            pred = "FAKE" if r["prob_fake"] >= threshold else "REAL"
            actual = "FAKE" if r["label"] == 1 else "REAL"
            mark = "+" if pred == actual else "X"
            print(f"    {mark} {r['filename'][:50]:50s} {actual:4s}→{pred:4s} p={r['prob_fake']:.4f} [{r['speaker']}]")


def compute_train_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {
        "accuracy": (preds == labels).mean(),
        "balanced_accuracy": balanced_accuracy_score(labels, preds),
    }


def train_v41(train_clips, val_clips, device, feature_extractor):
    n_real = sum(1 for c in train_clips if c["label"] == 0)
    n_fake = sum(1 for c in train_clips if c["label"] == 1)
    n_total = len(train_clips)

    print(f"\n{'=' * 70}")
    print(f"TRAINING V4.1 (data-diversity, NO class weights)")
    print(f"  Train: {n_total} clips ({n_real} real, {n_fake} fake)")
    print(f"  Val: {len(val_clips)} clips")
    print(f"{'=' * 70}")

    model = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    model.to(device)

    for param in model.wav2vec2.feature_extractor.parameters():
        param.requires_grad = False
    for param in model.wav2vec2.feature_projection.parameters():
        param.requires_grad = False
    for i in range(12):
        for param in model.wav2vec2.encoder.layers[i].parameters():
            param.requires_grad = False

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_p = sum(p.numel() for p in model.parameters())
    print(f"  Trainable: {trainable:,} / {total_p:,} ({100*trainable/total_p:.1f}%)")

    train_ds = AudioDataset(train_clips, feature_extractor, augment=True)
    val_ds = AudioDataset(val_clips, feature_extractor, augment=False)

    output_dir = os.path.join(MODELS_DIR, "wav2vec2-v41-diverse")
    epochs = 15 if n_total <= 100 else 10

    args = TrainingArguments(
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

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=compute_train_metrics,
    )

    print(f"  Training {epochs} epochs...")
    trainer.train()

    model.save_pretrained(output_dir)
    feature_extractor.save_pretrained(output_dir)
    print(f"  Model saved to {output_dir}")
    return model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--skip-training", action="store_true")
    args = parser.parse_args()

    device = torch.device(args.device)
    print(f"Device: {device}")

    # ── Assemble dataset ──
    print(f"\n{'=' * 70}")
    print("SCANNING DATASETS")
    print(f"{'=' * 70}")

    original = scan_original_samples()
    diverse_nat = scan_diverse_voices()
    xtts_clones = scan_xtts_clones()

    print(f"  Original V4: {len(original)} clips")
    print(f"  Diverse naturals: {len(diverse_nat)} clips ({len(set(c['speaker'] for c in diverse_nat))} speakers)")
    print(f"  XTTS clones: {len(xtts_clones)} clips ({len(set(c['speaker'] for c in xtts_clones))} speakers)")

    splits = build_splits(original, diverse_nat, xtts_clones)

    print(f"\n  Voice-disjoint splits:")
    for name, clips in splits.items():
        n_r = sum(1 for c in clips if c["label"] == 0)
        n_f = sum(1 for c in clips if c["label"] == 1)
        speakers = sorted(set(c["speaker"] for c in clips))
        print(f"    {name:25s}: {len(clips):3d} clips ({n_r} real, {n_f} fake) speakers={speakers}")

    train_speakers_actual = set(c["speaker"] for c in splits["train"] + splits["val"])
    test_speakers_actual = set(c["speaker"] for c in splits["test_unseen_synthetic"] + splits["test_unseen_natural"])
    leak = train_speakers_actual & test_speakers_actual
    if leak:
        print(f"\n  ⚠ SPEAKER LEAK: {leak}")
        return
    else:
        print(f"\n  ✓ No speaker leak between train and test")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    # ── Step 1: Run V4-A (frozen) on NEW test sets as baseline ──
    print(f"\n{'#' * 70}")
    print("# STEP 1: V4-A BASELINE on unseen test sets (BEFORE retraining)")
    print(f"{'#' * 70}")

    all_eval = {}

    model_paths = {
        "V4-A (control)": os.path.join(MODELS_DIR, "wav2vec2-v4a-no-weights"),
        "Deployed (control)": os.path.join(MODELS_DIR, "wav2vec2-heavy-augmented"),
    }

    for model_name, model_path in model_paths.items():
        if not os.path.exists(model_path):
            print(f"  {model_name}: NOT FOUND at {model_path}")
            continue

        print(f"\n{'━' * 70}")
        print(f"  MODEL: {model_name}")
        print(f"{'━' * 70}")

        model = AutoModelForAudioClassification.from_pretrained(model_path)
        model.to(device).eval()

        model_results = {}

        for test_name, test_clips in splits.items():
            if test_name in ("train",):
                continue
            res = evaluate_clips(model, feature_extractor, test_clips, device)
            m = compute_metrics_dict(res)
            model_results[test_name] = {"metrics": m, "per_clip": res}
            show_clips = test_name in ("test_unseen_synthetic", "test_unseen_natural", "jakub_holdout")
            print_metrics(f"{test_name} ({len(test_clips)} clips)", m,
                         per_clip=res if show_clips else None)

        all_eval[model_name] = model_results
        del model

    # ── Step 2: Train V4.1 ──
    if not args.skip_training:
        print(f"\n{'#' * 70}")
        print("# STEP 2: TRAIN V4.1")
        print(f"{'#' * 70}")
        model_v41 = train_v41(splits["train"], splits["val"], device, feature_extractor)
        del model_v41

    # ── Step 3: Evaluate V4.1 ──
    v41_path = os.path.join(MODELS_DIR, "wav2vec2-v41-diverse")
    if os.path.exists(v41_path):
        print(f"\n{'#' * 70}")
        print("# STEP 3: EVALUATE V4.1 on all test sets")
        print(f"{'#' * 70}")

        model_v41 = AutoModelForAudioClassification.from_pretrained(v41_path)
        model_v41.to(device).eval()

        v41_results = {}
        for test_name, test_clips in splits.items():
            if test_name in ("train",):
                continue
            res = evaluate_clips(model_v41, feature_extractor, test_clips, device)
            m = compute_metrics_dict(res)
            v41_results[test_name] = {"metrics": m, "per_clip": res}
            show_clips = test_name in ("test_unseen_synthetic", "test_unseen_natural", "jakub_holdout")
            print_metrics(f"{test_name} ({len(test_clips)} clips)", m,
                         per_clip=res if show_clips else None)

        all_eval["V4.1"] = v41_results
        del model_v41

    # ── Final comparison ──
    print(f"\n{'=' * 70}")
    print("FINAL COMPARISON TABLE")
    print(f"{'=' * 70}")

    model_names = list(all_eval.keys())
    test_sets = ["val", "jakub_holdout", "test_unseen_synthetic", "test_unseen_natural"]
    labels_map = {
        "val": "1. In-distribution val",
        "jakub_holdout": "2. Same-speaker holdout",
        "test_unseen_synthetic": "3. Unseen synthetic (KEY)",
        "test_unseen_natural": "4. Unseen human speaker",
    }

    header = f"  {'Test Set':30s}"
    for mn in model_names:
        header += f" {mn:>22s}"
    print(header)
    print("  " + "-" * (30 + 23 * len(model_names)))

    for ts in test_sets:
        label = labels_map.get(ts, ts)
        row = f"  {label:30s}"
        for mn in model_names:
            if mn in all_eval and ts in all_eval[mn]:
                m = all_eval[mn][ts]["metrics"]
                ba = m.get("balanced_accuracy", 0)
                fa = m.get("fake_accuracy", 0)
                ra = m.get("real_accuracy", 0)
                row += f"  BA={ba:5.1%} F={fa:4.0%} R={ra:4.0%}"
            else:
                row += f" {'N/A':>22s}"
        print(row)

    # Unseen synthetic per-speaker breakdown
    print(f"\n  Unseen synthetic per-speaker:")
    for mn in model_names:
        if mn not in all_eval or "test_unseen_synthetic" not in all_eval[mn]:
            continue
        clips = all_eval[mn]["test_unseen_synthetic"]["per_clip"]
        by_spk = defaultdict(list)
        for r in clips:
            by_spk[r["speaker"]].append(r)
        print(f"    {mn}:")
        for spk in sorted(by_spk.keys()):
            spk_clips = by_spk[spk]
            detected = sum(1 for r in spk_clips if r["prob_fake"] >= 0.5)
            probs = [r["prob_fake"] for r in spk_clips]
            print(f"      {spk}: {detected}/{len(spk_clips)} detected, "
                  f"p_fake=[{', '.join(f'{p:.3f}' for p in probs)}]")

    # ── Save results ──
    save_data = {
        "timestamp": timestamp,
        "device": str(device),
        "experiment": "v4.1-data-diversity",
        "train_speakers": sorted(TRAIN_SPEAKERS),
        "test_speakers": sorted(TEST_SPEAKERS),
        "splits": {k: len(v) for k, v in splits.items()},
    }
    for mn in all_eval:
        save_data[mn] = {}
        for ts, data in all_eval[mn].items():
            save_data[mn][ts] = {
                "metrics": data["metrics"],
                "per_clip": data["per_clip"],
            }

    results_path = os.path.join(RESULTS_DIR, f"v41_{timestamp}.json")
    with open(results_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
