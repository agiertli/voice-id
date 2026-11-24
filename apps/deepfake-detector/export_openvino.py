"""
Export a Wav2Vec2 deepfake detector (HF checkpoint dir) to ONNX and OpenVINO IR (FP32)
for OpenVINO Model Server, and verify IR logits == PyTorch logits.

Usage (in a venv with torch CPU, transformers<5, onnx, openvino):
    python export_openvino.py --model-dir models/wav2vec2-v4a-no-weights \
        --out ~/voice-id-audio/ovms/repo/gary-v2

Produces (OVMS model repository layout):
    <out>/1/model.xml
    <out>/1/model.bin
    <out>.onnx                (intermediate, kept next to the repo dir)

Model weights stay OUT of git (size). Write the output outside the working tree.

Contract (see docs/OPENVINO-DETECTOR.md):
    input  "input_values"  FP32 [batch, seq]   (Gary 2.0: seq = 160000; Gary base image: native clip length)
    output "logits"        FP32 [batch, 2]     index 1 = "fake"
The attention mask is not an input: serve.py always pads to 160000 *before* the feature
extractor, so the mask is all ones and has no effect.
"""

import argparse
import os
import sys

import numpy as np
import torch
from transformers import AutoModelForAudioClassification


class LogitsOnly(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_values):
        return self.m(input_values=input_values).logits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", required=True, help="OVMS model dir (version 1 is created inside)")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(os.path.join(out, "1"), exist_ok=True)
    onnx_path = out + ".onnx"

    model = AutoModelForAudioClassification.from_pretrained(args.model_dir, attn_implementation="eager")
    model.eval()
    wrapped = LogitsOnly(model).eval()

    dummy = torch.randn(1, 160000)
    with torch.no_grad():
        torch.onnx.export(
            wrapped, (dummy,), onnx_path,
            input_names=["input_values"], output_names=["logits"],
            dynamic_axes={"input_values": {0: "batch", 1: "sequence"}, "logits": {0: "batch"}},
            opset_version=args.opset, do_constant_folding=True, dynamo=False,
        )
    print(f"ONNX written: {onnx_path}")

    import openvino as ov
    ov_model = ov.convert_model(onnx_path)  # same as `ovc model.onnx`
    xml_path = os.path.join(out, "1", "model.xml")
    ov.save_model(ov_model, xml_path, compress_to_fp16=False)  # FP32, no quantization
    print(f"IR written: {xml_path}")

    # Parity: PyTorch vs ONNX-derived IR on CPU
    core = ov.Core()
    compiled = core.compile_model(xml_path, "CPU", {"INFERENCE_PRECISION_HINT": "f32"})
    rng = np.random.default_rng(0)
    worst = 0.0
    cases = []
    for i in range(4):
        x = rng.standard_normal((1, 160000)).astype(np.float32)
        cases.append(("randn", x))
    # realistic: speech-like burst padded with zeros, then zero-mean/unit-var normalized
    for n in (16000 * 2, 16000 * 6):
        a = np.zeros(160000, np.float32)
        t = np.arange(n) / 16000.0
        a[:n] = 0.05 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * rng.standard_normal(n))
        a = (a - a.mean()) / np.sqrt(a.var() + 1e-7)
        cases.append((f"padded{n//16000}s", a[None, :].astype(np.float32)))
    cases.append(("batch2", np.concatenate([cases[0][1], cases[-1][1]], axis=0)))
    cases.append(("seq48000", rng.standard_normal((1, 48000)).astype(np.float32)))
    for name, x in cases:
        with torch.no_grad():
            ref = wrapped(torch.from_numpy(x)).numpy()
        got = compiled(x)[0]
        d = float(np.max(np.abs(ref - got)))
        pr = torch.softmax(torch.from_numpy(ref), -1).numpy()[:, 1]
        pg = torch.softmax(torch.from_numpy(got), -1).numpy()[:, 1]
        worst = max(worst, d)
        print(f"  {name:10s} max|dlogit|={d:.2e}  p_fake torch={np.round(pr,5)} ov={np.round(pg,5)}")
    print(f"WORST max|dlogit| = {worst:.2e}")
    if worst >= 1e-3:
        print("PARITY FAILED", file=sys.stderr)
        sys.exit(2)
    print("PARITY OK")


if __name__ == "__main__":
    main()
