"""
OVMS adapter: a drop-in replacement for serve.py's HTTP API, backed by OpenVINO Model Server.

It keeps the API identical so the Voice ID game needs no code change:
    POST /predict   multipart field "audio" -> {"verdict", "confidence", "prob_fake", "duration"}
    GET  /health    liveness  -> {"status": "ok"}
    GET  /ready     readiness -> {"status": "ready"} when the OVMS model is ready, else 503

It does serve.py's preprocessing (decode, mono, resample to 16 kHz, RMS normalisation, length policy,
zero-mean/unit-variance), calls OVMS over KServe v2 REST with the binary tensor extension, and applies
serve.py's softmax + thresholds. There are no model weights in this image.

Environment:
    OVMS_URL        e.g. http://gary-finetuned-predictor.voice-id.svc.cluster.local:8888
                    (the predictor Service is headless, so use the pod port 8888, not the advertised :80)
    OVMS_MODEL      OVMS model name (= InferenceService name), e.g. gary-finetuned
    INPUT_LENGTH    "160000" (Gary 2.0, current serve.py) | "native" (Gary base image, older serve.py)
                    default: ovms_client.MODEL_LENGTH lookup by OVMS_MODEL
    LOW_THRESHOLD / HIGH_THRESHOLD   default 0.3 / 0.7 (same as serve.py)
    OVMS_TIMEOUT    seconds, default 60
    READY_GRACE_S   /ready answers from a successful OVMS call within this many seconds (or in-flight work)
                    instead of probing OVMS, so readiness does not flap under load; default 60
"""

import io
import logging
import os
import threading
import time
import urllib.request

import numpy as np
import soundfile as sf
from flask import Flask, jsonify, request

import ovms_client as oc

OVMS_URL = os.environ.get("OVMS_URL", "http://localhost:8888").rstrip("/")
OVMS_MODEL = os.environ.get("OVMS_MODEL", "gary-finetuned")
_len = os.environ.get("INPUT_LENGTH", "").strip().lower()
INPUT_LENGTH = (oc.MODEL_LENGTH.get(OVMS_MODEL, oc.MAX_LENGTH) if _len in ("", "auto")
                else None if _len == "native" else int(_len))
OVMS_TIMEOUT = float(os.environ.get("OVMS_TIMEOUT", "60"))
# Readiness must not flap under load: while OVMS is busy with inferences its REST workers may not answer
# /v2/models/<m>/ready quickly, which would pull the adapter out of the Service exactly when it is needed.
# So /ready is answered from recent evidence: a successful OVMS response within READY_GRACE_S seconds,
# or requests currently in flight. Only otherwise it probes OVMS.
READY_GRACE_S = float(os.environ.get("READY_GRACE_S", "60"))
_state = {"last_ok": 0.0, "inflight": 0}
_lock = threading.Lock()


def _mark_ok():
    with _lock:
        _state["last_ok"] = time.monotonic()

app = Flask(__name__)
log = logging.getLogger("ovms-adapter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log.info("OVMS adapter: url=%s model=%s input_length=%s thresholds=%s/%s",
         OVMS_URL, OVMS_MODEL, INPUT_LENGTH or "native", oc.LOW_THRESHOLD, oc.HIGH_THRESHOLD)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ready")
def ready():
    with _lock:
        recent = time.monotonic() - _state["last_ok"] < READY_GRACE_S
        busy = _state["inflight"] > 0
    if recent or busy:
        return jsonify({"status": "ready"})
    try:
        with urllib.request.urlopen(f"{OVMS_URL}/v2/models/{OVMS_MODEL}/ready", timeout=3) as r:
            if r.status == 200:
                _mark_ok()
                return jsonify({"status": "ready"})
    except Exception as e:  # noqa: BLE001
        log.warning("OVMS not ready: %r", e)
    return jsonify({"status": "not ready"}), 503


@app.route("/predict", methods=["POST"])
def predict():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided. Use multipart form field 'audio'."}), 400
    data = request.files["audio"].read()
    t0 = time.perf_counter()
    try:
        # Same decoder as serve.py (libsndfile, float32)
        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not decode audio: {e}"}), 400
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    x, duration = oc.preprocess(audio, sr, INPUT_LENGTH)
    t1 = time.perf_counter()
    with _lock:
        _state["inflight"] += 1
    try:
        logits = oc.infer(OVMS_URL, OVMS_MODEL, x, binary=True, timeout=OVMS_TIMEOUT)
        _mark_ok()
    except Exception as e:  # noqa: BLE001
        log.error("OVMS inference failed: %r", e)
        return jsonify({"error": f"OVMS inference failed: {e}"}), 502
    finally:
        with _lock:
            _state["inflight"] -= 1
    t2 = time.perf_counter()
    r = oc.postprocess(logits, duration)
    r.pop("prob_fake_raw", None)
    log.info("predict verdict=%s p_fake=%.4f samples=%d pre_ms=%.0f ovms_ms=%.0f",
             r["verdict"], r["prob_fake"], x.shape[1], (t1 - t0) * 1000, (t2 - t1) * 1000)
    return jsonify(r)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
