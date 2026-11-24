"""
Thin client for the Voice ID deepfake detectors served by OpenVINO Model Server (KServe v2 REST).

Reproduces serve.py's preprocessing exactly, then calls OVMS and applies serve.py's
post-processing. Returns the same JSON shape as serve.py's POST /predict:
    {"verdict", "confidence", "prob_fake", "duration"}

Dependencies: stdlib + numpy + scipy (scipy.signal.resample_poly - the exact resampler serve.py
uses; the game backend image already ships scipy). Non-WAV input is converted with ffmpeg,
the same way the game does (convert_to_wav_pcm: mono, 22050 Hz, s16).

CLI:
    python ovms_client.py --url http://localhost:8888 --model gary-finetuned clip.wav [...]
    python ovms_client.py --json-payload ...      # plain JSON tensor instead of binary extension
    python ovms_client.py --length native ...     # no 10 s truncate/pad (what the Gary base image does)

Payload contract: docs/OPENVINO-DETECTOR.md
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10  # 160000 - must match training
TARGET_RMS = 0.05
LOW_THRESHOLD = float(os.environ.get("LOW_THRESHOLD", "0.3"))
HIGH_THRESHOLD = float(os.environ.get("HIGH_THRESHOLD", "0.7"))
INPUT_NAME = "input_values"
OUTPUT_NAME = "logits"
FAKE_INDEX = 1  # config.json id2label: {0: real, 1: fake}

# Input length policy per deployed model. The two running detector images use DIFFERENT serve.py versions:
#   gary-finetuned  quay.io/agiertli/deepfake-detector:v2.0.0      -> current serve.py: truncate/pad to 160000
#   gary-base       quay.io/agiertli/deepfake-detector-base:v1.0.3 -> older serve.py: NO truncate/pad, the whole clip
#                                                                    at native length (feature extractor padding=True
#                                                                    on a single input = no padding)
# None = native length (dynamic sequence axis), int = fixed length.
MODEL_LENGTH = {"gary-finetuned": MAX_LENGTH, "gary-base": None}


# ---------- decode (serve.py: sf.read(dtype=float32) -> mono -> resample_poly) ----------

def to_wav_pcm(audio_bytes):
    """Same ffmpeg call as the game's convert_to_wav_pcm (mono, 22050 Hz, s16 WAV)."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    with tempfile.NamedTemporaryFile(suffix=".audio") as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        proc = subprocess.run([ffmpeg, "-i", tmp.name, "-ac", "1", "-ar", "22050",
                               "-sample_fmt", "s16", "-f", "wav", "pipe:1"], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[-200:]}")
    return proc.stdout


def decode_wav(wav_bytes):
    """PCM WAV -> (float32 mono samples in [-1, 1), sample_rate). Equals soundfile float32 read."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / np.float32(32768.0)
    elif width == 4:
        a = (np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    elif width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / np.float32(128.0)
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a.astype(np.float32), sr


def load_audio(audio_bytes):
    if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        try:
            return decode_wav(audio_bytes)
        except (wave.Error, ValueError):
            pass  # e.g. float/extensible WAV -> let ffmpeg normalise it
    return decode_wav(to_wav_pcm(audio_bytes))


# ---------- preprocessing (serve.py predict(), line for line) ----------

def preprocess(audio, sr, length=MAX_LENGTH):
    """Returns (input_values float32 [1, length or native], duration_seconds).

    length=MAX_LENGTH reproduces the current serve.py (Gary 2.0); length=None reproduces the older serve.py
    baked into the Gary base image (no truncation/padding)."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        g = gcd(SAMPLE_RATE, sr)
        audio = resample_poly(audio, SAMPLE_RATE // g, sr // g).astype(np.float32)
    # normalize_audio: RMS -> 0.05, clip to [-1, 1]
    rms = np.sqrt(np.mean(audio ** 2))
    if rms > 1e-6:
        audio = audio * (TARGET_RMS / rms)
    audio = np.clip(audio, -1.0, 1.0)
    duration = len(audio) / SAMPLE_RATE
    # preprocess_audio: truncate / right-pad with zeros to exactly `length` (skipped when length is None)
    audio = audio.astype(np.float32)
    if length is not None:
        if len(audio) > length:
            audio = audio[:length]
        elif len(audio) < length:
            audio = np.pad(audio, (0, length - len(audio)))
    # Wav2Vec2FeatureExtractor(do_normalize=True): zero-mean / unit-variance over ALL samples sent
    # (with fixed length the zero padding is included, because the mask is all ones after the pad above)
    x = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
    return x.astype(np.float32)[None, :], duration


# ---------- OVMS KServe v2 call ----------

def infer(url, model, input_values, binary=True, timeout=60):
    """POST /v2/models/<model>/infer. Returns logits float32 [batch, 2]."""
    endpoint = f"{url.rstrip('/')}/v2/models/{model}/infer"
    shape = list(input_values.shape)
    if binary:
        # KServe v2 binary tensor extension (supported by OVMS)
        payload = input_values.astype("<f4").tobytes()
        header = json.dumps({
            "inputs": [{"name": INPUT_NAME, "shape": shape, "datatype": "FP32",
                        "parameters": {"binary_data_size": len(payload)}}],
            "outputs": [{"name": OUTPUT_NAME}],
        }).encode()
        body = header + payload
        headers = {"Content-Type": "application/octet-stream",
                   "Inference-Header-Content-Length": str(len(header))}
    else:
        body = json.dumps({
            "inputs": [{"name": INPUT_NAME, "shape": shape, "datatype": "FP32",
                        "data": input_values.ravel().tolist()}],
        }).encode()
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        hdr_len = resp.headers.get("Inference-Header-Content-Length")
    if hdr_len is not None:  # binary response
        hdr_len = int(hdr_len)
        meta = json.loads(raw[:hdr_len])
        out = meta["outputs"][0]
        data = np.frombuffer(raw[hdr_len:hdr_len + out["parameters"]["binary_data_size"]], dtype="<f4")
    else:
        meta = json.loads(raw)
        out = next(o for o in meta["outputs"] if o["name"] == OUTPUT_NAME)
        data = np.asarray(out["data"], dtype=np.float32)
    return data.reshape(out["shape"])


# ---------- post-processing (serve.py) ----------

def postprocess(logits, duration):
    l = np.asarray(logits, dtype=np.float32)[0]
    e = np.exp(l - l.max())
    prob_fake = float((e / e.sum())[FAKE_INDEX])
    if prob_fake < LOW_THRESHOLD:
        verdict, confidence = "REAL", 1.0 - prob_fake
    elif prob_fake > HIGH_THRESHOLD:
        verdict, confidence = "SYNTHETIC", prob_fake
    else:
        verdict, confidence = "UNCERTAIN", 1.0 - abs(prob_fake - 0.5) * 2
    return {"verdict": verdict, "confidence": round(confidence, 4),
            "prob_fake": round(prob_fake, 4), "duration": round(duration, 1),
            "prob_fake_raw": prob_fake}


def predict(audio_bytes, url, model, binary=True, timeout=60, length="auto"):
    """audio bytes (any format ffmpeg reads) -> serve.py-compatible result dict.
    length: "auto" (MODEL_LENGTH lookup, default 160000), an int, or None (native length)."""
    if length == "auto":
        length = MODEL_LENGTH.get(model, MAX_LENGTH)
    audio, sr = load_audio(audio_bytes)
    x, duration = preprocess(audio, sr, length)
    return postprocess(infer(url, model, x, binary=binary, timeout=timeout), duration)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("OVMS_URL", "http://localhost:8888"))
    ap.add_argument("--model", default="gary-finetuned")
    ap.add_argument("--json-payload", action="store_true", help="JSON tensor instead of binary")
    ap.add_argument("--length", default="auto", help="auto | native | <samples> (default: per-model MODEL_LENGTH)")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    length = {"auto": "auto", "native": None}.get(a.length) if a.length in ("auto", "native") else int(a.length)
    for f in a.files:
        with open(f, "rb") as fh:
            t = time.perf_counter()
            r = predict(fh.read(), a.url, a.model, binary=not a.json_payload, length=length)
        r.pop("prob_fake_raw")
        r["file"], r["ms"] = os.path.basename(f), round((time.perf_counter() - t) * 1000)
        print(json.dumps(r))


if __name__ == "__main__":
    sys.exit(main())
