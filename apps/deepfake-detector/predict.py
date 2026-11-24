"""
Predict whether an audio clip is real or AI-generated.

Uses the fine-tuned wav2vec2 deepfake voice detector.

Usage:
    python predict.py audio.wav
    python predict.py audio1.wav audio2.wav audio3.wav
    python predict.py --model base audio.wav          # Use pretrained (no fine-tuning)
    python predict.py --threshold 0.3 0.7 audio.wav   # Custom UNCERTAIN band

Output: REAL / UNCERTAIN / SYNTHETIC with confidence score.

Calibration:
    p_fake < low_threshold  → REAL (high confidence)
    p_fake > high_threshold → SYNTHETIC (high confidence)
    otherwise               → UNCERTAIN
"""

import os
import sys
import argparse
import warnings
warnings.filterwarnings("ignore")

import torch
import librosa
import numpy as np
from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINETUNED_MODEL = os.path.join(BASE_DIR, "models", "wav2vec2-heavy")
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"
SAMPLE_RATE = 16000
TARGET_RMS = 0.05


def normalize_audio(audio):
    current_rms = np.sqrt(np.mean(audio ** 2))
    if current_rms > 1e-6:
        audio = audio * (TARGET_RMS / current_rms)
    return np.clip(audio, -1.0, 1.0)


def predict_clip(model, feature_extractor, audio_path, device):
    audio, sr = librosa.load(audio_path, sr=SAMPLE_RATE, mono=True)
    audio = normalize_audio(audio)
    duration = len(audio) / SAMPLE_RATE

    inputs = feature_extractor(
        audio, sampling_rate=SAMPLE_RATE, return_tensors="pt", padding=True,
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)
        prob_fake = probs[0][1].item()

    return {
        "prob_fake": prob_fake,
        "duration": duration,
    }


def classify(prob_fake, low_thresh, high_thresh):
    if prob_fake < low_thresh:
        return "REAL", 1.0 - prob_fake
    elif prob_fake > high_thresh:
        return "SYNTHETIC", prob_fake
    else:
        return "UNCERTAIN", 1.0 - abs(prob_fake - 0.5) * 2


def main():
    parser = argparse.ArgumentParser(description="Deepfake voice detector")
    parser.add_argument("files", nargs="+", help="Audio files to analyze")
    parser.add_argument("--model", default="finetuned",
                        choices=["finetuned", "base"],
                        help="Which model to use")
    parser.add_argument("--threshold", nargs=2, type=float, default=[0.3, 0.7],
                        metavar=("LOW", "HIGH"),
                        help="UNCERTAIN band thresholds (default: 0.3 0.7)")
    parser.add_argument("--device", default="cpu", help="cpu, mps, or cuda")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    low_thresh, high_thresh = args.threshold

    model_path = FINETUNED_MODEL if args.model == "finetuned" else BASE_MODEL
    if args.model == "finetuned" and not os.path.exists(model_path):
        print(f"Fine-tuned model not found at {model_path}")
        print("Run experiment.py first, or use --model base")
        sys.exit(1)

    device = torch.device(args.device)
    feature_extractor = AutoFeatureExtractor.from_pretrained(
        BASE_MODEL if args.model == "base" else model_path
    )
    model = AutoModelForAudioClassification.from_pretrained(model_path)
    model.to(device)
    model.eval()

    results = []
    for fpath in args.files:
        if not os.path.exists(fpath):
            print(f"File not found: {fpath}", file=sys.stderr)
            continue

        pred = predict_clip(model, feature_extractor, fpath, device)
        verdict, confidence = classify(pred["prob_fake"], low_thresh, high_thresh)

        result = {
            "file": os.path.basename(fpath),
            "verdict": verdict,
            "confidence": round(confidence, 3),
            "prob_fake": round(pred["prob_fake"], 4),
            "duration": round(pred["duration"], 1),
        }
        results.append(result)

        if not args.json:
            icon = {"REAL": "+", "SYNTHETIC": "!", "UNCERTAIN": "?"}[verdict]
            print(f"  [{icon}] {result['file']:45s} "
                  f"{verdict:10s} conf={confidence:.1%} "
                  f"p_fake={pred['prob_fake']:.4f} "
                  f"({pred['duration']:.1f}s)")

    if args.json:
        import json
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
