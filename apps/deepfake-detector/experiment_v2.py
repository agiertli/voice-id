"""
Experiment v2: Improved evaluation framework for Voice ID deepfake detector.

Implements:
  Phase 1  — Strict train/validation/locked-test evaluation
  Phase 1.5 — Shortcut baselines (duration, silence, spectral, VAD)
  Phase 2  — Unified preprocessing

Key differences from experiment.py:
  - Locked test set is NEVER evaluated during training or model selection
  - Unified preprocessing function used everywhere
  - Shortcut baselines run first to detect dataset artifacts
  - Threshold selected on validation only
  - All results include raw counts, not just percentages

Usage:
    python experiment_v2.py                         # Full pipeline
    python experiment_v2.py --phase shortcuts       # Phase 1.5 only
    python experiment_v2.py --phase baseline        # Phase 1 baseline only
    python experiment_v2.py --phase train           # Phase 1 train + validate
    python experiment_v2.py --phase test            # Locked test (run ONCE)
    python experiment_v2.py --device mps            # Use Apple Silicon GPU
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
import soundfile as sf
import torch
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, roc_auc_score,
    confusion_matrix, classification_report,
)
from sklearn.preprocessing import StandardScaler
from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(BASE_DIR, "experiment_manifest.json")
RESULTS_DIR = os.path.join(BASE_DIR, "results", "v2")
MODELS_DIR = os.path.join(BASE_DIR, "models")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"

SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10


# ============================================================
# Phase 2: Unified preprocessing
# ============================================================

def preprocess_audio(audio, sample_rate=SAMPLE_RATE, max_length=MAX_LENGTH):
    """Canonical preprocessing — used for training, evaluation, AND inference.

    Returns: 1D numpy array, float32, 16kHz, padded/truncated to max_length.
    """
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)

    # Pad or truncate to fixed length
    if len(audio) > max_length:
        audio = audio[:max_length]
    elif len(audio) < max_length:
        audio = np.pad(audio, (0, max_length - len(audio)))

    return audio


def extract_features_for_model(audio, feature_extractor):
    """Run feature extractor with consistent settings."""
    inputs = feature_extractor(
        audio,
        sampling_rate=SAMPLE_RATE,
        return_tensors="pt",
        padding="max_length",
        max_length=MAX_LENGTH,
        truncation=True,
    )
    return inputs


# ============================================================
# Phase 1.5: Audio feature extraction for shortcut analysis
# ============================================================

def compute_silence_features(audio, sr=SAMPLE_RATE, silence_thresh_db=-40):
    """Compute silence/VAD features — the most likely shortcut."""
    frame_length = int(0.025 * sr)  # 25ms frames
    hop_length = int(0.010 * sr)    # 10ms hop

    # Frame-level energy in dB
    frames = librosa.util.frame(audio, frame_length=frame_length, hop_length=hop_length)
    frame_energy = np.sum(frames ** 2, axis=0)
    frame_energy_db = 10 * np.log10(frame_energy + 1e-10)

    # Voice activity: frames above threshold
    is_speech = frame_energy_db > silence_thresh_db
    n_frames = len(is_speech)

    if n_frames == 0:
        return {
            "vad_ratio": 0, "silence_ratio": 1,
            "n_speech_segments": 0, "n_pauses": 0,
            "mean_pause_dur": 0, "max_pause_dur": 0,
            "leading_silence": 0, "trailing_silence": 0,
        }

    vad_ratio = np.mean(is_speech)

    # Find speech segments and pauses
    changes = np.diff(is_speech.astype(int))
    seg_starts = np.where(changes == 1)[0] + 1
    seg_ends = np.where(changes == -1)[0] + 1

    if is_speech[0]:
        seg_starts = np.concatenate([[0], seg_starts])
    if is_speech[-1]:
        seg_ends = np.concatenate([seg_ends, [n_frames]])

    n_speech_segments = len(seg_starts)

    # Pauses between speech segments
    pause_durs = []
    for i in range(len(seg_ends) - 1):
        if i < len(seg_starts) - 1:
            pause_frames = seg_starts[i + 1] - seg_ends[i]
            if pause_frames > 0:
                pause_durs.append(pause_frames * hop_length / sr)

    # Leading and trailing silence
    first_speech = seg_starts[0] if len(seg_starts) > 0 else n_frames
    last_speech = seg_ends[-1] if len(seg_ends) > 0 else 0
    leading_silence = first_speech * hop_length / sr
    trailing_silence = (n_frames - last_speech) * hop_length / sr

    return {
        "vad_ratio": float(vad_ratio),
        "silence_ratio": float(1 - vad_ratio),
        "n_speech_segments": n_speech_segments,
        "n_pauses": len(pause_durs),
        "mean_pause_dur": float(np.mean(pause_durs)) if pause_durs else 0,
        "max_pause_dur": float(np.max(pause_durs)) if pause_durs else 0,
        "leading_silence": float(leading_silence),
        "trailing_silence": float(trailing_silence),
    }


def compute_spectral_features(audio, sr=SAMPLE_RATE):
    """Compute spectral features for shortcut detection."""
    spectral_centroid = librosa.feature.spectral_centroid(y=audio, sr=sr)[0]
    spectral_bandwidth = librosa.feature.spectral_bandwidth(y=audio, sr=sr)[0]
    spectral_rolloff = librosa.feature.spectral_rolloff(y=audio, sr=sr)[0]
    spectral_flatness = librosa.feature.spectral_flatness(y=audio)[0]
    zcr = librosa.feature.zero_crossing_rate(audio)[0]

    return {
        "spectral_centroid_mean": float(np.mean(spectral_centroid)),
        "spectral_centroid_std": float(np.std(spectral_centroid)),
        "spectral_bandwidth_mean": float(np.mean(spectral_bandwidth)),
        "spectral_bandwidth_std": float(np.std(spectral_bandwidth)),
        "spectral_rolloff_mean": float(np.mean(spectral_rolloff)),
        "spectral_flatness_mean": float(np.mean(spectral_flatness)),
        "spectral_flatness_std": float(np.std(spectral_flatness)),
        "zcr_mean": float(np.mean(zcr)),
        "zcr_std": float(np.std(zcr)),
    }


def compute_basic_features(audio, sr=SAMPLE_RATE):
    """Compute basic audio features."""
    duration = len(audio) / sr
    # Compute on non-padded portion
    nonzero_end = len(audio)
    for i in range(len(audio) - 1, -1, -1):
        if abs(audio[i]) > 1e-8:
            nonzero_end = i + 1
            break
    actual_audio = audio[:nonzero_end]
    actual_duration = len(actual_audio) / sr

    rms = float(np.sqrt(np.mean(actual_audio ** 2))) if len(actual_audio) > 0 else 0
    peak = float(np.max(np.abs(actual_audio))) if len(actual_audio) > 0 else 0

    return {
        "duration": actual_duration,
        "rms": rms,
        "peak_amplitude": peak,
        "crest_factor": peak / rms if rms > 1e-6 else 0,
    }


def extract_all_features(clip):
    """Extract all shortcut-analysis features for one clip."""
    audio, sr = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)

    features = {}
    features.update(compute_basic_features(audio, sr))
    features.update(compute_silence_features(audio, sr))
    features.update(compute_spectral_features(audio, sr))

    return features


# ============================================================
# Phase 1.5: Shortcut baseline classifiers
# ============================================================

def run_shortcut_baselines(train_clips, val_clips, test_clips=None):
    """Train simple classifiers to detect dataset shortcuts."""
    print("\n" + "=" * 70)
    print("PHASE 1.5: SHORTCUT BASELINE ANALYSIS")
    print("=" * 70)

    # Extract features
    print("\nExtracting audio features...")
    train_features = [extract_all_features(c) for c in train_clips]
    val_features = [extract_all_features(c) for c in val_clips]

    train_labels = np.array([c["label"] for c in train_clips])
    val_labels = np.array([c["label"] for c in val_clips])

    # Build feature matrix
    feature_names = sorted(train_features[0].keys())
    X_train = np.array([[f[k] for k in feature_names] for f in train_features])
    X_val = np.array([[f[k] for k in feature_names] for f in val_features])

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled = scaler.transform(X_val)

    results = {}

    # --- Individual feature analysis ---
    print("\n--- Individual Feature Discrimination ---")
    print(f"{'Feature':35s} {'Real mean':>10s} {'Fake mean':>10s} {'Gap':>8s} {'LR val acc':>10s}")
    print("-" * 75)

    feature_scores = []
    for i, fname in enumerate(feature_names):
        real_vals = X_train[train_labels == 0, i]
        fake_vals = X_train[train_labels == 1, i]
        gap = abs(np.mean(real_vals) - np.mean(fake_vals))

        # Single-feature logistic regression
        lr = LogisticRegression(max_iter=1000, random_state=42)
        lr.fit(X_train_scaled[:, i:i+1], train_labels)
        val_acc = lr.score(X_val_scaled[:, i:i+1], val_labels)
        val_pred = lr.predict(X_val_scaled[:, i:i+1])
        val_correct = int(np.sum(val_pred == val_labels))

        feature_scores.append((fname, val_acc, val_correct, len(val_labels)))

        print(f"  {fname:33s} {np.mean(real_vals):10.4f} {np.mean(fake_vals):10.4f} "
              f"{gap:8.4f} {val_correct}/{len(val_labels)} = {val_acc:.0%}")

    # Sort by validation accuracy
    feature_scores.sort(key=lambda x: x[1], reverse=True)
    results["individual_features"] = [
        {"feature": f, "val_accuracy": a, "val_correct": c, "val_total": t}
        for f, a, c, t in feature_scores
    ]

    # --- Multi-feature classifiers ---
    print("\n--- Multi-Feature Classifiers ---")

    classifiers = {
        "LogisticRegression (all features)": LogisticRegression(max_iter=1000, random_state=42),
        "DecisionTree (depth=3)": DecisionTreeClassifier(max_depth=3, random_state=42),
        "RandomForest (10 trees)": RandomForestClassifier(n_estimators=10, max_depth=3, random_state=42),
    }

    # Also test specific feature subsets
    feature_subsets = {
        "duration_only": ["duration"],
        "silence_vad_only": ["vad_ratio", "silence_ratio", "n_pauses", "mean_pause_dur",
                             "leading_silence", "trailing_silence"],
        "spectral_only": ["spectral_centroid_mean", "spectral_bandwidth_mean",
                          "spectral_flatness_mean", "zcr_mean"],
    }

    clf_results = []
    for clf_name, clf in classifiers.items():
        clf.fit(X_train_scaled, train_labels)
        val_pred = clf.predict(X_val_scaled)
        val_acc = accuracy_score(val_labels, val_pred)
        val_correct = int(np.sum(val_pred == val_labels))

        real_correct = int(np.sum((val_pred == val_labels) & (val_labels == 0)))
        real_total = int(np.sum(val_labels == 0))
        fake_correct = int(np.sum((val_pred == val_labels) & (val_labels == 1)))
        fake_total = int(np.sum(val_labels == 1))

        print(f"\n  {clf_name}:")
        print(f"    Val: {val_correct}/{len(val_labels)} = {val_acc:.0%} "
              f"(REAL: {real_correct}/{real_total}, FAKE: {fake_correct}/{fake_total})")

        clf_results.append({
            "classifier": clf_name, "features": "all",
            "val_accuracy": val_acc, "val_correct": val_correct, "val_total": len(val_labels),
            "val_real_correct": real_correct, "val_real_total": real_total,
            "val_fake_correct": fake_correct, "val_fake_total": fake_total,
        })

        # Feature importances for tree-based
        if hasattr(clf, "feature_importances_"):
            importances = sorted(zip(feature_names, clf.feature_importances_),
                                 key=lambda x: x[1], reverse=True)
            print(f"    Top features: {', '.join(f'{n}={v:.3f}' for n, v in importances[:5])}")

    # Test feature subsets
    for subset_name, subset_features in feature_subsets.items():
        indices = [feature_names.index(f) for f in subset_features if f in feature_names]
        if not indices:
            continue

        lr = LogisticRegression(max_iter=1000, random_state=42)
        lr.fit(X_train_scaled[:, indices], train_labels)
        val_pred = lr.predict(X_val_scaled[:, indices])
        val_acc = accuracy_score(val_labels, val_pred)
        val_correct = int(np.sum(val_pred == val_labels))

        real_correct = int(np.sum((val_pred == val_labels) & (val_labels == 0)))
        real_total = int(np.sum(val_labels == 0))
        fake_correct = int(np.sum((val_pred == val_labels) & (val_labels == 1)))
        fake_total = int(np.sum(val_labels == 1))

        print(f"\n  LR ({subset_name}):")
        print(f"    Val: {val_correct}/{len(val_labels)} = {val_acc:.0%} "
              f"(REAL: {real_correct}/{real_total}, FAKE: {fake_correct}/{fake_total})")

        clf_results.append({
            "classifier": f"LR_{subset_name}", "features": subset_name,
            "val_accuracy": val_acc, "val_correct": val_correct, "val_total": len(val_labels),
            "val_real_correct": real_correct, "val_real_total": real_total,
            "val_fake_correct": fake_correct, "val_fake_total": fake_total,
        })

    results["classifiers"] = clf_results

    # --- Per-clip feature dump for the false positive investigation ---
    print("\n--- Feature Values for Key Clips ---")
    all_clips = train_clips + val_clips
    all_features_list = train_features + val_features

    # Find the Miska false positive
    for clip, feats in zip(all_clips, all_features_list):
        if "fa779860" in clip["filename"]:
            print(f"\n  *** MISKA FALSE POSITIVE: {clip['filename']} ***")
            for k, v in sorted(feats.items()):
                print(f"    {k:35s} = {v:.4f}")

    # Compare means: real vs fake
    print("\n--- Feature Distribution Summary ---")
    real_feats = [f for f, c in zip(all_features_list, all_clips) if c["label"] == 0]
    fake_feats = [f for f, c in zip(all_features_list, all_clips) if c["label"] == 1]

    print(f"\n  {'Feature':35s} {'Real':>12s} {'Fake':>12s} {'Diff':>10s}")
    print("  " + "-" * 72)
    for fname in feature_names:
        r_vals = [f[fname] for f in real_feats]
        f_vals = [f[fname] for f in fake_feats]
        r_mean = np.mean(r_vals)
        f_mean = np.mean(f_vals)
        diff = f_mean - r_mean
        print(f"  {fname:35s} {r_mean:12.4f} {f_mean:12.4f} {diff:+10.4f}")

    results["feature_distributions"] = {
        "real": {fname: {"mean": float(np.mean([f[fname] for f in real_feats])),
                         "std": float(np.std([f[fname] for f in real_feats]))}
                 for fname in feature_names},
        "fake": {fname: {"mean": float(np.mean([f[fname] for f in fake_feats])),
                         "std": float(np.std([f[fname] for f in fake_feats]))}
                 for fname in feature_names},
    }

    return results


# ============================================================
# Phase 1: Neural model evaluation (strict methodology)
# ============================================================

def evaluate_model(model, feature_extractor, clips, device, split_name=""):
    """Evaluate model with unified preprocessing. Returns detailed results."""
    model.eval()
    results = []

    for clip in clips:
        audio, sr = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)
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
            "label_name": clip["label_name"],
            "source": clip["source"],
            "language": clip["language"],
            "prob_fake": float(prob_fake),
        })

    return results


def compute_metrics_at_threshold(results, threshold=0.5):
    """Compute classification metrics at a given threshold."""
    labels = np.array([r["label"] for r in results])
    probs = np.array([r["prob_fake"] for r in results])
    preds = (probs >= threshold).astype(int)

    acc = accuracy_score(labels, preds)
    bal_acc = balanced_accuracy_score(labels, preds)

    try:
        auc = roc_auc_score(labels, probs)
    except ValueError:
        auc = None

    cm = confusion_matrix(labels, preds, labels=[0, 1])
    tn, fp, fn, tp = cm.ravel()

    real_total = int(np.sum(labels == 0))
    fake_total = int(np.sum(labels == 1))
    real_correct = int(tn)
    fake_correct = int(tp)
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0
    fnr = fn / (fn + tp) if (fn + tp) > 0 else 0

    # Per-source breakdown
    by_source = defaultdict(lambda: {"correct": 0, "total": 0, "probs": []})
    for r in results:
        pred = 1 if r["prob_fake"] >= threshold else 0
        correct = pred == r["label"]
        by_source[r["source"]]["total"] += 1
        if correct:
            by_source[r["source"]]["correct"] += 1
        by_source[r["source"]]["probs"].append(r["prob_fake"])

    by_source_summary = {}
    for src, stats in sorted(by_source.items()):
        by_source_summary[src] = {
            "correct": stats["correct"],
            "total": stats["total"],
            "accuracy": stats["correct"] / stats["total"],
            "mean_prob_fake": float(np.mean(stats["probs"])),
        }

    return {
        "threshold": threshold,
        "accuracy": acc,
        "balanced_accuracy": bal_acc,
        "roc_auc": auc,
        "total_correct": int(np.sum(preds == labels)),
        "total": len(labels),
        "real_correct": real_correct,
        "real_total": real_total,
        "fake_correct": fake_correct,
        "fake_total": fake_total,
        "fpr": fpr,
        "fnr": fnr,
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        "by_source": by_source_summary,
    }


def find_best_threshold(val_results, thresholds=None):
    """Find threshold that maximizes balanced accuracy on validation."""
    if thresholds is None:
        thresholds = np.arange(0.1, 0.95, 0.05)

    best_threshold = 0.5
    best_bal_acc = 0

    for t in thresholds:
        metrics = compute_metrics_at_threshold(val_results, threshold=t)
        if metrics["balanced_accuracy"] > best_bal_acc:
            best_bal_acc = metrics["balanced_accuracy"]
            best_threshold = t

    return best_threshold


def print_evaluation(metrics, split_name, per_clip=None):
    """Print evaluation results with raw counts."""
    print(f"\n  [{split_name}] threshold={metrics['threshold']:.2f}")
    print(f"  Accuracy:  {metrics['total_correct']}/{metrics['total']} = {metrics['accuracy']:.1%}")
    print(f"  Balanced:  {metrics['balanced_accuracy']:.1%}")
    if metrics['roc_auc'] is not None:
        print(f"  ROC-AUC:   {metrics['roc_auc']:.3f}")
    print(f"  REAL:      {metrics['real_correct']}/{metrics['real_total']} = "
          f"{metrics['real_correct']/metrics['real_total']:.0%}" if metrics['real_total'] > 0 else "")
    print(f"  FAKE:      {metrics['fake_correct']}/{metrics['fake_total']} = "
          f"{metrics['fake_correct']/metrics['fake_total']:.0%}" if metrics['fake_total'] > 0 else "")
    print(f"  FPR (REAL→FAKE): {metrics['fpr']:.1%}")
    print(f"  FNR (FAKE→REAL): {metrics['fnr']:.1%}")
    print(f"  Confusion: TP={metrics['confusion_matrix']['tp']} FP={metrics['confusion_matrix']['fp']} "
          f"FN={metrics['confusion_matrix']['fn']} TN={metrics['confusion_matrix']['tn']}")

    print(f"  Per-source:")
    for src, stats in sorted(metrics["by_source"].items()):
        print(f"    {src:25s}: {stats['correct']}/{stats['total']} = {stats['accuracy']:.0%} "
              f"(mean p_fake={stats['mean_prob_fake']:.3f})")

    if per_clip:
        print(f"  Per-clip:")
        for r in per_clip:
            pred = "FAKE" if r["prob_fake"] >= metrics["threshold"] else "REAL"
            actual = "FAKE" if r["label"] == 1 else "REAL"
            correct = pred == actual
            mark = "+" if correct else "X"
            print(f"    {mark} {r['filename'][:45]:45s} "
                  f"actual={actual:4s} pred={pred:4s} p_fake={r['prob_fake']:.4f}")


def run_baseline_evaluation(manifest, device):
    """Phase 1: Evaluate base and fine-tuned models with strict methodology."""
    print("\n" + "=" * 70)
    print("PHASE 1: BASELINE EVALUATION (strict methodology)")
    print("=" * 70)

    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    models_to_eval = {
        "base": BASE_MODEL,
        "heavy": os.path.join(MODELS_DIR, "wav2vec2-heavy"),
    }

    all_results = {}

    for model_name, model_path in models_to_eval.items():
        if not os.path.exists(model_path) and model_name != "base":
            print(f"\n  Skipping {model_name} — model not found at {model_path}")
            continue

        print(f"\n{'─' * 50}")
        print(f"Model: {model_name} ({model_path})")
        print(f"{'─' * 50}")

        model = AutoModelForAudioClassification.from_pretrained(model_path)
        model.to(device)
        model.eval()

        # Evaluate on train and validation (NOT test)
        train_results = evaluate_model(model, feature_extractor, manifest["train"], device, "train")
        val_results = evaluate_model(model, feature_extractor, manifest["val"], device, "val")

        # Find best threshold on VALIDATION only
        best_threshold = find_best_threshold(val_results)
        print(f"\n  Best validation threshold: {best_threshold:.2f}")

        # Report with best threshold
        train_metrics = compute_metrics_at_threshold(train_results, threshold=best_threshold)
        val_metrics = compute_metrics_at_threshold(val_results, threshold=best_threshold)

        print_evaluation(train_metrics, "TRAIN")
        print_evaluation(val_metrics, "VALIDATION", per_clip=val_results)

        # Also report at default 0.5 for comparison
        val_metrics_05 = compute_metrics_at_threshold(val_results, threshold=0.5)
        print(f"\n  [Comparison at threshold=0.50] Val: {val_metrics_05['total_correct']}/{val_metrics_05['total']} = {val_metrics_05['accuracy']:.1%}")

        all_results[model_name] = {
            "model_path": model_path,
            "best_threshold": best_threshold,
            "train": train_metrics,
            "validation": val_metrics,
            "validation_at_05": val_metrics_05,
            "train_per_clip": train_results,
            "val_per_clip": val_results,
        }

        del model
        if device.type == "mps":
            torch.mps.empty_cache()
        elif device.type == "cuda":
            torch.cuda.empty_cache()

    return all_results


def run_locked_test(manifest, device, model_name, threshold):
    """Phase 1 locked test: evaluate ONCE with frozen model and threshold."""
    print("\n" + "=" * 70)
    print("LOCKED TEST EVALUATION")
    print(f"Model: {model_name}, Threshold: {threshold:.2f}")
    print("WARNING: This should be run ONCE. Do not use results to make decisions.")
    print("=" * 70)

    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    model_path = BASE_MODEL if model_name == "base" else os.path.join(MODELS_DIR, f"wav2vec2-{model_name}")
    model = AutoModelForAudioClassification.from_pretrained(model_path)
    model.to(device)
    model.eval()

    test_results = evaluate_model(model, feature_extractor, manifest["held_out"], device, "locked_test")
    test_metrics = compute_metrics_at_threshold(test_results, threshold=threshold)

    print_evaluation(test_metrics, "LOCKED TEST", per_clip=test_results)

    return {
        "model": model_name,
        "model_path": model_path,
        "threshold": threshold,
        "metrics": test_metrics,
        "per_clip": test_results,
    }


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Voice ID Deepfake Detector — Experiment v2")
    parser.add_argument("--device", default="mps", help="cpu, mps, or cuda")
    parser.add_argument("--phase", default="all",
                        choices=["all", "shortcuts", "baseline", "test"],
                        help="Which phase to run")
    parser.add_argument("--test-model", default="heavy",
                        help="Model name for locked test (base or heavy)")
    parser.add_argument("--test-threshold", type=float, default=None,
                        help="Threshold for locked test (must be set from validation)")
    args = parser.parse_args()

    device = torch.device(args.device)
    print(f"Device: {device}")

    # Load manifest
    if not os.path.exists(MANIFEST_PATH):
        print("ERROR: Run prepare_experiment.py first!")
        return

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    print(f"Dataset: {len(manifest['train'])} train, "
          f"{len(manifest['val'])} val, "
          f"{len(manifest['held_out'])} held-out")

    os.makedirs(RESULTS_DIR, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    all_results = {"timestamp": timestamp, "device": str(device)}

    # Phase 1.5: Shortcut baselines
    if args.phase in ("all", "shortcuts"):
        shortcut_results = run_shortcut_baselines(manifest["train"], manifest["val"])
        all_results["shortcuts"] = shortcut_results

        # Save shortcut results
        with open(os.path.join(RESULTS_DIR, f"shortcuts_{timestamp}.json"), "w") as f:
            json.dump(shortcut_results, f, indent=2, default=str)
        print(f"\nShortcut results saved to results/v2/shortcuts_{timestamp}.json")

    # Phase 1: Baseline evaluation
    if args.phase in ("all", "baseline"):
        baseline_results = run_baseline_evaluation(manifest, device)
        all_results["baseline"] = baseline_results

        # Save baseline results
        with open(os.path.join(RESULTS_DIR, f"baseline_{timestamp}.json"), "w") as f:
            json.dump(baseline_results, f, indent=2, default=str)
        print(f"\nBaseline results saved to results/v2/baseline_{timestamp}.json")

    # Locked test — only when explicitly requested
    if args.phase == "test":
        if args.test_threshold is None:
            print("\nERROR: --test-threshold must be set from validation results.")
            print("Example: python experiment_v2.py --phase test --test-model heavy --test-threshold 0.45")
            return
        test_results = run_locked_test(manifest, device, args.test_model, args.test_threshold)
        all_results["locked_test"] = test_results

        with open(os.path.join(RESULTS_DIR, f"locked_test_{timestamp}.json"), "w") as f:
            json.dump(test_results, f, indent=2, default=str)
        print(f"\nLocked test results saved to results/v2/locked_test_{timestamp}.json")

    # Save combined results
    if args.phase == "all":
        with open(os.path.join(RESULTS_DIR, f"experiment_v2_{timestamp}.json"), "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"\nFull results saved to results/v2/experiment_v2_{timestamp}.json")


if __name__ == "__main__":
    main()
