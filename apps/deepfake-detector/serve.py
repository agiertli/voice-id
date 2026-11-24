"""
Flask inference server for the fine-tuned deepfake voice detector.

Endpoints:
    POST /predict       — upload audio file, get REAL/UNCERTAIN/SYNTHETIC verdict
    GET  /health        — liveness probe
    GET  /ready         — readiness probe (model loaded)

Usage:
    python serve.py                              # Development
    gunicorn --bind 0.0.0.0:8080 serve:app       # Production
"""

import os
import io
import tempfile
import warnings
warnings.filterwarnings("ignore")

import torch
import soundfile as sf
import numpy as np
from scipy.signal import resample_poly
from math import gcd
from flask import Flask, request, jsonify
from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

app = Flask(__name__)

MODEL_DIR = os.environ.get("MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "wav2vec2-v4a-no-weights"))
BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"
SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10  # 160000 — must match training
TARGET_RMS = 0.05
LOW_THRESHOLD = float(os.environ.get("LOW_THRESHOLD", "0.3"))
HIGH_THRESHOLD = float(os.environ.get("HIGH_THRESHOLD", "0.7"))

model = None
feature_extractor = None


def load_model():
    global model, feature_extractor
    print(f"Loading model from {MODEL_DIR}...")
    feature_extractor = AutoFeatureExtractor.from_pretrained(MODEL_DIR)
    model = AutoModelForAudioClassification.from_pretrained(MODEL_DIR)
    model.eval()
    print("Model loaded.")


def normalize_audio(audio):
    current_rms = np.sqrt(np.mean(audio ** 2))
    if current_rms > 1e-6:
        audio = audio * (TARGET_RMS / current_rms)
    return np.clip(audio, -1.0, 1.0)


def preprocess_audio(audio):
    """Truncate/pad to MAX_LENGTH — must match training pipeline exactly."""
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)
    if len(audio) > MAX_LENGTH:
        audio = audio[:MAX_LENGTH]
    elif len(audio) < MAX_LENGTH:
        audio = np.pad(audio, (0, MAX_LENGTH - len(audio)))
    return audio


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ready")
def ready():
    if model is None:
        return jsonify({"status": "not ready"}), 503
    return jsonify({"status": "ready"})


@app.route("/predict", methods=["POST"])
def predict():
    if model is None:
        return jsonify({"error": "Model not loaded"}), 503

    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided. Use multipart form field 'audio'."}), 400

    audio_file = request.files["audio"]

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        audio_file.save(tmp.name)
        audio, sr = sf.read(tmp.name, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SAMPLE_RATE:
            g = gcd(SAMPLE_RATE, sr)
            audio = resample_poly(audio, SAMPLE_RATE // g, sr // g).astype(np.float32)

    audio = normalize_audio(audio)
    duration = len(audio) / SAMPLE_RATE
    audio = preprocess_audio(audio)

    inputs = feature_extractor(
        audio, sampling_rate=SAMPLE_RATE, return_tensors="pt",
        padding="max_length", max_length=MAX_LENGTH, truncation=True,
    )

    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)
        prob_fake = probs[0][1].item()

    if prob_fake < LOW_THRESHOLD:
        verdict = "REAL"
        confidence = 1.0 - prob_fake
    elif prob_fake > HIGH_THRESHOLD:
        verdict = "SYNTHETIC"
        confidence = prob_fake
    else:
        verdict = "UNCERTAIN"
        confidence = 1.0 - abs(prob_fake - 0.5) * 2

    return jsonify({
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "prob_fake": round(prob_fake, 4),
        "duration": round(duration, 1),
    })


load_model()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
