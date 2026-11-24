"""
Step 3: Three-regime fine-tuning experiment.

Compares:
  1. BASE     — garystafford's pretrained model, zero training
  2. LIGHT    — freeze all except projector + classifier, 5 epochs, lr=2e-5
  3. HEAVY    — freeze CNN + bottom 12 layers (Gary's approach), 20 epochs, lr=1e-5

For each regime:
  - Evaluates on train, val, AND held-out sets
  - Tracks per-epoch metrics to show overfitting onset
  - Computes per-source accuracy and confidence calibration

Run: python experiment.py [--device mps|cpu|cuda]

Prerequisites: python prepare_experiment.py (creates manifest + normalized clips)
"""

import os
import json
import copy
import argparse
import warnings
warnings.filterwarnings("ignore")

import torch
import librosa
import numpy as np
from torch.utils.data import Dataset
from transformers import (
    AutoFeatureExtractor,
    AutoModelForAudioClassification,
    Trainer,
    TrainingArguments,
)
from sklearn.metrics import roc_auc_score, confusion_matrix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(BASE_DIR, "experiment_manifest.json")
MODELS_DIR = os.path.join(BASE_DIR, "models")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"
SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10


class AudioDataset(Dataset):
    def __init__(self, clips, feature_extractor):
        self.clips = clips
        self.feature_extractor = feature_extractor

    def __len__(self):
        return len(self.clips)

    def __getitem__(self, idx):
        clip = self.clips[idx]
        audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)
        inputs = self.feature_extractor(
            audio, sampling_rate=SAMPLE_RATE, return_tensors="pt",
            padding="max_length", max_length=MAX_LENGTH, truncation=True,
        )
        item = {k: v.squeeze(0) for k, v in inputs.items()}
        item["labels"] = torch.tensor(clip["label"], dtype=torch.long)
        return item


def evaluate_split(model, feature_extractor, clips, device, split_name=""):
    """Evaluate model on a set of clips. Returns detailed per-clip results."""
    model.eval()
    results = []

    for clip in clips:
        audio, _ = librosa.load(clip["path"], sr=SAMPLE_RATE, mono=True)
        inputs = feature_extractor(
            audio, sampling_rate=SAMPLE_RATE, return_tensors="pt", padding=True,
        )
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)
            prob_fake = probs[0][1].item()
            pred = 1 if prob_fake >= 0.5 else 0

        results.append({
            "filename": clip["filename"],
            "label": clip["label"],
            "label_name": clip["label_name"],
            "source": clip["source"],
            "language": clip["language"],
            "pred": pred,
            "prob_fake": prob_fake,
            "correct": pred == clip["label"],
        })

    # Compute metrics
    labels = [r["label"] for r in results]
    preds = [r["pred"] for r in results]
    probs = [r["prob_fake"] for r in results]

    accuracy = sum(r["correct"] for r in results) / len(results)

    try:
        auc = roc_auc_score(labels, probs)
    except ValueError:
        auc = None

    cm = confusion_matrix(labels, preds, labels=[0, 1])

    # Per-source accuracy
    by_source = {}
    for r in results:
        key = r["source"]
        by_source.setdefault(key, {"correct": 0, "total": 0, "probs": []})
        by_source[key]["total"] += 1
        if r["correct"]:
            by_source[key]["correct"] += 1
        by_source[key]["probs"].append(r["prob_fake"])

    for key in by_source:
        s = by_source[key]
        s["accuracy"] = s["correct"] / s["total"]
        s["mean_prob_fake"] = float(np.mean(s["probs"]))
        s["std_prob_fake"] = float(np.std(s["probs"]))
        del s["probs"]

    metrics = {
        "split": split_name,
        "accuracy": accuracy,
        "auc": auc,
        "confusion_matrix": cm.tolist(),
        "n_clips": len(results),
        "by_source": by_source,
    }

    return metrics, results


def print_evaluation(metrics, per_clip, verbose=True):
    """Print evaluation results."""
    print(f"\n  [{metrics['split']}] Accuracy: {metrics['accuracy']:.1%} "
          f"({int(metrics['accuracy'] * metrics['n_clips'])}/{metrics['n_clips']})"
          f"  AUC: {metrics['auc']:.3f}" if metrics['auc'] else "")

    cm = metrics["confusion_matrix"]
    print(f"  Confusion matrix: TP={cm[1][1]} FP={cm[0][1]} FN={cm[1][0]} TN={cm[0][0]}")

    for source, stats in sorted(metrics["by_source"].items()):
        print(f"    {source:20s}: {stats['accuracy']:.0%} "
              f"({stats['correct']}/{stats['total']}) "
              f"p_fake={stats['mean_prob_fake']:.3f}±{stats['std_prob_fake']:.3f}")

    if verbose:
        print(f"  Per-clip:")
        for r in per_clip:
            mark = "+" if r["correct"] else "X"
            actual = "fake" if r["label"] == 1 else "real"
            pred = "fake" if r["pred"] == 1 else "real"
            print(f"    {mark} {r['filename'][:45]:45s} "
                  f"actual={actual:4s} pred={pred:4s} p_fake={r['prob_fake']:.3f}")


def freeze_for_regime(model, regime):
    """Apply freezing strategy for a given regime."""
    if regime == "base":
        return

    if regime == "light":
        # Freeze everything EXCEPT projector + classifier
        for name, param in model.named_parameters():
            if "projector" in name or "classifier" in name:
                param.requires_grad = True
            else:
                param.requires_grad = False

    elif regime == "heavy":
        # Gary Stafford's approach: freeze CNN + bottom 12 transformer layers
        for name, param in model.named_parameters():
            param.requires_grad = True

        # Freeze feature extractor (CNN)
        for param in model.wav2vec2.feature_extractor.parameters():
            param.requires_grad = False
        for param in model.wav2vec2.feature_projection.parameters():
            param.requires_grad = False

        # Freeze bottom 12 of 24 transformer layers
        for i in range(12):
            for param in model.wav2vec2.encoder.layers[i].parameters():
                param.requires_grad = False

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"  Trainable: {trainable:,} / {total:,} ({100*trainable/total:.1f}%)")


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    acc = (preds == labels).mean()
    return {"accuracy": acc}


class EpochCallback(torch.nn.Module):
    """Not a real nn.Module — just a Trainer callback for tracking per-epoch."""
    pass


def run_regime(regime, config, manifest, device, feature_extractor):
    """Run a single training regime and return all results."""
    print(f"\n{'='*70}")
    print(f"REGIME: {regime.upper()}")
    print(f"{'='*70}")

    model = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    model.to(device)

    freeze_for_regime(model, regime)

    train_clips = manifest["train"]
    val_clips = manifest["val"]
    held_out_clips = manifest["held_out"]

    # --- Baseline (before training) ---
    print(f"\n--- Before training ---")
    train_m, train_r = evaluate_split(model, feature_extractor, train_clips, device, "train")
    val_m, val_r = evaluate_split(model, feature_extractor, val_clips, device, "val")
    held_m, held_r = evaluate_split(model, feature_extractor, held_out_clips, device, "held_out")

    print_evaluation(train_m, train_r, verbose=False)
    print_evaluation(val_m, val_r, verbose=False)
    print_evaluation(held_m, held_r, verbose=True)

    results = {
        "regime": regime,
        "config": config,
        "baseline": {
            "train": train_m,
            "val": val_m,
            "held_out": held_m,
            "held_out_per_clip": held_r,
        },
        "epochs": [],
    }

    if regime == "base":
        return results, model

    # --- Training ---
    train_dataset = AudioDataset(train_clips, feature_extractor)
    val_dataset = AudioDataset(val_clips, feature_extractor)

    output_dir = os.path.join(MODELS_DIR, f"wav2vec2-{regime}")

    training_args = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=config["epochs"],
        per_device_train_batch_size=config.get("batch_size", 2),
        per_device_eval_batch_size=2,
        learning_rate=config["lr"],
        weight_decay=config.get("weight_decay", 0.01),
        warmup_steps=2,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        logging_steps=5,
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

    # Train and evaluate after each epoch
    print(f"\n--- Training {config['epochs']} epochs ---")

    # We'll train epoch by epoch to capture per-epoch held-out metrics
    for epoch in range(1, config["epochs"] + 1):
        print(f"\n  Epoch {epoch}/{config['epochs']}...")

        # Train for 1 epoch
        one_epoch_args = TrainingArguments(
            output_dir=output_dir,
            num_train_epochs=1,
            per_device_train_batch_size=config.get("batch_size", 2),
            per_device_eval_batch_size=2,
            learning_rate=config["lr"],
            weight_decay=config.get("weight_decay", 0.01),
            warmup_steps=2 if epoch == 1 else 0,
            eval_strategy="no",
            save_strategy="no",
            logging_steps=100,
            fp16=False,
            dataloader_num_workers=0,
            use_cpu=(str(device) == "cpu"),
            report_to="none",
        )

        epoch_trainer = Trainer(
            model=model,
            args=one_epoch_args,
            train_dataset=train_dataset,
            compute_metrics=compute_metrics,
        )
        epoch_trainer.train()

        # Evaluate on all splits
        tr_m, tr_r = evaluate_split(model, feature_extractor, train_clips, device, "train")
        va_m, va_r = evaluate_split(model, feature_extractor, val_clips, device, "val")
        ho_m, ho_r = evaluate_split(model, feature_extractor, held_out_clips, device, "held_out")

        epoch_data = {
            "epoch": epoch,
            "train_acc": tr_m["accuracy"],
            "val_acc": va_m["accuracy"],
            "held_out_acc": ho_m["accuracy"],
            "train_auc": tr_m["auc"],
            "val_auc": va_m["auc"],
            "held_out_auc": ho_m["auc"],
            "train_by_source": tr_m["by_source"],
            "val_by_source": va_m["by_source"],
            "held_out_by_source": ho_m["by_source"],
        }
        results["epochs"].append(epoch_data)

        print(f"    Train: {tr_m['accuracy']:.1%}  Val: {va_m['accuracy']:.1%}  "
              f"Held-out: {ho_m['accuracy']:.1%}")

        # Check for perfect train accuracy (overfitting signal)
        if tr_m["accuracy"] == 1.0 and epoch >= 3:
            gap = tr_m["accuracy"] - ho_m["accuracy"]
            if gap > 0.15:
                print(f"    ⚠ Overfitting detected: train-held_out gap = {gap:.1%}")

    # Final detailed evaluation
    print(f"\n--- Final evaluation (epoch {config['epochs']}) ---")
    train_m, train_r = evaluate_split(model, feature_extractor, train_clips, device, "train")
    val_m, val_r = evaluate_split(model, feature_extractor, val_clips, device, "val")
    held_m, held_r = evaluate_split(model, feature_extractor, held_out_clips, device, "held_out")

    print_evaluation(train_m, train_r, verbose=False)
    print_evaluation(val_m, val_r, verbose=False)
    print_evaluation(held_m, held_r, verbose=True)

    results["final"] = {
        "train": train_m,
        "val": val_m,
        "held_out": held_m,
        "held_out_per_clip": held_r,
    }

    # Save model
    model.save_pretrained(output_dir)
    feature_extractor.save_pretrained(output_dir)
    print(f"\n  Model saved to {output_dir}")

    return results, model


def print_comparison_table(all_results):
    """Print a comparison table of all regimes."""
    print(f"\n{'='*70}")
    print(f"COMPARISON SUMMARY")
    print(f"{'='*70}")

    header = f"{'Regime':12s} | {'Train':8s} | {'Val':8s} | {'Held-out':8s} | {'Overfit?':10s}"
    print(header)
    print("-" * len(header))

    for res in all_results:
        regime = res["regime"].upper()
        if regime == "BASE":
            tr = res["baseline"]["train"]["accuracy"]
            va = res["baseline"]["val"]["accuracy"]
            ho = res["baseline"]["held_out"]["accuracy"]
        else:
            tr = res["final"]["train"]["accuracy"]
            va = res["final"]["val"]["accuracy"]
            ho = res["final"]["held_out"]["accuracy"]

        gap = tr - ho
        overfit = "YES" if gap > 0.15 else "mild" if gap > 0.05 else "no"

        print(f"{regime:12s} | {tr:7.1%} | {va:7.1%} | {ho:7.1%} | {overfit:10s}")

    # Per-source breakdown
    print(f"\nPer-source held-out accuracy:")
    sources = set()
    for res in all_results:
        stage = res.get("final", res["baseline"])
        sources.update(stage["held_out"]["by_source"].keys())

    header2 = f"{'Regime':12s} | " + " | ".join(f"{s[:12]:12s}" for s in sorted(sources))
    print(header2)
    print("-" * len(header2))

    for res in all_results:
        regime = res["regime"].upper()
        stage = res.get("final", res["baseline"])
        by_src = stage["held_out"]["by_source"]
        vals = []
        for s in sorted(sources):
            if s in by_src:
                vals.append(f"{by_src[s]['accuracy']:11.0%} ")
            else:
                vals.append(f"{'N/A':>12s}")
        print(f"{regime:12s} | " + " | ".join(vals))

    # Overfitting analysis
    print(f"\nOverfitting curves (train_acc - held_out_acc gap by epoch):")
    for res in all_results:
        if not res["epochs"]:
            continue
        regime = res["regime"].upper()
        print(f"  {regime}:")
        for ep in res["epochs"]:
            gap = ep["train_acc"] - ep["held_out_acc"]
            bar = "#" * int(gap * 50)
            print(f"    Epoch {ep['epoch']:2d}: train={ep['train_acc']:.1%} "
                  f"val={ep['val_acc']:.1%} held={ep['held_out_acc']:.1%} "
                  f"gap={gap:+.1%} {bar}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="mps",
                        help="cpu, mps (Apple Silicon), or cuda")
    args = parser.parse_args()

    device = torch.device(args.device)
    print(f"Device: {device}")

    if not os.path.exists(MANIFEST_PATH):
        print("Run prepare_experiment.py first!")
        return

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    print(f"Train: {len(manifest['train'])} clips")
    print(f"Val: {len(manifest['val'])} clips")
    print(f"Held-out: {len(manifest['held_out'])} clips")

    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)

    # Define regimes
    regimes = [
        ("base", {}),
        ("light", {"epochs": 5, "lr": 2e-5, "batch_size": 2, "weight_decay": 0.01}),
        ("heavy", {"epochs": 20, "lr": 1e-5, "batch_size": 2, "weight_decay": 0.01}),
    ]

    all_results = []
    for regime_name, config in regimes:
        res, model = run_regime(regime_name, config, manifest, device, feature_extractor)
        all_results.append(res)
        del model
        if device.type != "cpu":
            torch.mps.empty_cache() if device.type == "mps" else torch.cuda.empty_cache()

    # Print comparison
    print_comparison_table(all_results)

    # Save full results
    results_path = os.path.join(RESULTS_DIR, "experiment_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull results saved to {results_path}")


if __name__ == "__main__":
    main()
