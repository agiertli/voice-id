"""
Fine-tune garystafford/wav2vec2-deepfake-voice-detector on Voice ID game clips.

Teaches the model to recognize:
- Fish Audio S2 Pro artifacts (currently 50% miss rate)
- Phone-recorded human speech (currently 0% accuracy on Anton's m4a clips)

Usage:
    # Extract clips first
    python extract_clips.py

    # Fine-tune (CPU — slow but works, ~20 min for 10 epochs)
    python finetune.py

    # Fine-tune on GPU (much faster)
    python finetune.py --device cuda

    # Evaluate only (no training)
    python finetune.py --eval-only

Model is saved to models/wav2vec2-voiceid-finetuned/
"""

import os
import glob
import argparse
import json
import random

import torch
import librosa
import numpy as np
from sklearn.model_selection import train_test_split
from transformers import (
    AutoFeatureExtractor,
    AutoModelForAudioClassification,
    Trainer,
    TrainingArguments,
)
from torch.utils.data import Dataset

BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "models", "wav2vec2-voiceid-finetuned")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

SAMPLE_RATE = 16000


class AudioDataset(Dataset):
    def __init__(self, file_paths, labels, feature_extractor):
        self.file_paths = file_paths
        self.labels = labels
        self.feature_extractor = feature_extractor

    def __len__(self):
        return len(self.file_paths)

    def __getitem__(self, idx):
        audio, _ = librosa.load(self.file_paths[idx], sr=SAMPLE_RATE, mono=True)
        inputs = self.feature_extractor(
            audio, sampling_rate=SAMPLE_RATE, return_tensors="pt", padding="max_length",
            max_length=SAMPLE_RATE * 10, truncation=True,
        )
        item = {k: v.squeeze(0) for k, v in inputs.items()}
        item["labels"] = torch.tensor(self.labels[idx], dtype=torch.long)
        return item


def load_data():
    """Load WAV files from data/real/ and data/fake/ directories."""
    files, labels = [], []

    for wav in glob.glob(os.path.join(DATA_DIR, "real", "*.wav")):
        files.append(wav)
        labels.append(0)  # real

    for wav in glob.glob(os.path.join(DATA_DIR, "fake", "*.wav")):
        files.append(wav)
        labels.append(1)  # fake

    print(f"Loaded {len(files)} clips: {labels.count(0)} real, {labels.count(1)} fake")
    return files, labels


def evaluate(model, feature_extractor, files, labels, device, threshold=0.4):
    """Run evaluation and return per-category results."""
    model.eval()
    results = {"correct": 0, "total": 0, "by_cat": {}}

    for fpath, label in zip(files, labels):
        fname = os.path.basename(fpath)
        actual = "fake" if label == 1 else "real"

        # Determine category
        if label == 1:
            cat = "Fish" if "Fish" in fname or "fish" in fname.lower() else "XTTS"
        else:
            cat = "anton" if "anton" in fname else "miska"

        audio, _ = librosa.load(fpath, sr=SAMPLE_RATE, mono=True)
        inputs = feature_extractor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt", padding=True)
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            probs = torch.softmax(model(**inputs).logits, dim=-1)
            prob_fake = probs[0][1].item()
            pred = "fake" if prob_fake >= threshold else "real"

        ok = pred == actual
        results["total"] += 1
        if ok:
            results["correct"] += 1

        if cat not in results["by_cat"]:
            results["by_cat"][cat] = {"correct": 0, "total": 0}
        results["by_cat"][cat]["total"] += 1
        if ok:
            results["by_cat"][cat]["correct"] += 1

        mark = "+" if ok else "✗"
        print(f"  {mark} {fname[:45]:45s} actual={actual:4s} pred={pred:4s} p_fake={prob_fake:.3f}")

    return results


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    acc = (preds == labels).mean()
    return {"accuracy": acc}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu", help="cpu or cuda")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--eval-only", action="store_true")
    parser.add_argument("--model-path", default=None, help="Path to fine-tuned model for eval")
    args = parser.parse_args()

    files, labels = load_data()
    if len(files) == 0:
        print("No clips found. Run extract_clips.py first.")
        return

    # Stratified split: 80% train, 20% test
    train_files, test_files, train_labels, test_labels = train_test_split(
        files, labels, test_size=0.2, stratify=labels, random_state=42
    )
    print(f"Train: {len(train_files)} | Test: {len(test_files)}")

    # Load model
    model_name = args.model_path or (OUTPUT_DIR if args.eval_only and os.path.exists(OUTPUT_DIR) else BASE_MODEL)
    print(f"Loading model: {model_name}")
    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)
    model = AutoModelForAudioClassification.from_pretrained(model_name)
    model.to(args.device)

    if args.eval_only:
        print("\n=== EVALUATION (test set) ===")
        results = evaluate(model, feature_extractor, test_files, test_labels, args.device)
        print(f"\nTotal: {results['correct']}/{results['total']} ({100*results['correct']/results['total']:.0f}%)")
        for cat, v in sorted(results["by_cat"].items()):
            print(f"  {cat:10s}: {v['correct']}/{v['total']} ({100*v['correct']/v['total']:.0f}%)")

        print("\n=== EVALUATION (full dataset) ===")
        results = evaluate(model, feature_extractor, files, labels, args.device)
        print(f"\nTotal: {results['correct']}/{results['total']} ({100*results['correct']/results['total']:.0f}%)")
        for cat, v in sorted(results["by_cat"].items()):
            print(f"  {cat:10s}: {v['correct']}/{v['total']} ({100*v['correct']/v['total']:.0f}%)")
        return

    # Prepare datasets
    train_dataset = AudioDataset(train_files, train_labels, feature_extractor)
    test_dataset = AudioDataset(test_files, test_labels, feature_extractor)

    # Training config — conservative to avoid catastrophic forgetting on small dataset
    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=2,
        per_device_eval_batch_size=2,
        learning_rate=args.lr,
        weight_decay=0.01,
        warmup_ratio=0.1,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        logging_steps=5,
        fp16=False,
        dataloader_num_workers=0,
        use_cpu=(args.device == "cpu"),
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=test_dataset,
        compute_metrics=compute_metrics,
    )

    print("\n=== PRE-TRAINING BASELINE (test set) ===")
    baseline = evaluate(model, feature_extractor, test_files, test_labels, args.device)
    print(f"Baseline: {baseline['correct']}/{baseline['total']} ({100*baseline['correct']/baseline['total']:.0f}%)")

    print("\n=== TRAINING ===")
    trainer.train()

    print("\n=== POST-TRAINING EVALUATION (test set) ===")
    results = evaluate(model, feature_extractor, test_files, test_labels, args.device)
    print(f"\nTest: {results['correct']}/{results['total']} ({100*results['correct']/results['total']:.0f}%)")
    for cat, v in sorted(results["by_cat"].items()):
        print(f"  {cat:10s}: {v['correct']}/{v['total']} ({100*v['correct']/v['total']:.0f}%)")

    print("\n=== POST-TRAINING EVALUATION (full dataset) ===")
    full_results = evaluate(model, feature_extractor, files, labels, args.device)
    print(f"\nFull: {full_results['correct']}/{full_results['total']} ({100*full_results['correct']/full_results['total']:.0f}%)")
    for cat, v in sorted(full_results["by_cat"].items()):
        print(f"  {cat:10s}: {v['correct']}/{v['total']} ({100*v['correct']/v['total']:.0f}%)")

    # Save results
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "finetune_results.json"), "w") as f:
        json.dump({
            "baseline": baseline,
            "test": results,
            "full": full_results,
            "args": vars(args),
        }, f, indent=2)

    # Save model
    model.save_pretrained(OUTPUT_DIR)
    feature_extractor.save_pretrained(OUTPUT_DIR)
    print(f"\nModel saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
