"""
Parity test: current Flask detectors (serve.py /predict) vs OVMS + ovms_client.py, on a folder of clips.

Each clip goes through the game's exact path first (ffmpeg -> mono 22050 Hz s16 WAV), then the same
WAV bytes are sent to both backends.

    python ovms_parity.py --clips ~/voice-id-audio/ovms/clips \
        --pair gary-finetuned=http://localhost:18081,http://localhost:8888 \
        --pair gary-base=http://localhost:18082,http://localhost:8889 \
        --out ~/voice-id-audio/ovms/parity.csv

Clips are Rule Zero data: keep --clips and --out outside git and delete them afterwards.
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ovms_client  # noqa: E402

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".webm", ".aac"}


def flask_predict(url, wav_bytes):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav_bytes + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"{url}/predict", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True)
    ap.add_argument("--pair", action="append", required=True, help="model=flask_url,ovms_url")
    ap.add_argument("--out", required=True)
    ap.add_argument("--json-payload", action="store_true")
    a = ap.parse_args()

    files = sorted(os.path.join(d, f) for d, _, fs in os.walk(os.path.expanduser(a.clips)) for f in fs
                   if os.path.splitext(f)[1].lower() in AUDIO_EXT and "/speakers/" not in d + "/")
    pairs = []
    for p in a.pair:
        m, urls = p.split("=", 1)
        fu, ou = urls.split(",")
        pairs.append((m, fu, ou))
    print(f"{len(files)} clips, models: {[p[0] for p in pairs]}")

    rows, stats = [], {m: {"flips": [], "diff": [], "t_flask": [], "t_ovms": [], "fail": []} for m, _, _ in pairs}
    for i, f in enumerate(files):
        rel = os.path.relpath(f, os.path.expanduser(a.clips))
        with open(f, "rb") as fh:
            wav = ovms_client.to_wav_pcm(fh.read())  # identical to the game's convert_to_wav_pcm
        for m, fu, ou in pairs:
            s = stats[m]
            try:
                t = time.perf_counter(); ref = flask_predict(fu, wav); tf = time.perf_counter() - t
                t = time.perf_counter(); got = ovms_client.predict(wav, ou, m, binary=not a.json_payload); to = time.perf_counter() - t
            except Exception as e:  # noqa: BLE001
                s["fail"].append((rel, repr(e)))
                print(f"FAIL {m} {rel}: {e!r}")
                continue
            d = abs(ref["prob_fake"] - got["prob_fake_raw"])
            s["diff"].append(d); s["t_flask"].append(tf); s["t_ovms"].append(to)
            flip = ref["verdict"] != got["verdict"]
            if flip:
                s["flips"].append((rel, ref, got))
            rows.append({"model": m, "clip": rel, "flask_verdict": ref["verdict"], "ovms_verdict": got["verdict"],
                         "flask_p": ref["prob_fake"], "ovms_p": round(got["prob_fake_raw"], 6),
                         "abs_diff": round(d, 6), "flask_ms": round(tf * 1000), "ovms_ms": round(to * 1000),
                         "flask_dur": ref["duration"], "ovms_dur": got["duration"]})
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(files)}", flush=True)

    out = os.path.expanduser(a.out)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    for m, s in stats.items():
        print(f"\n== {m}: n={len(s['diff'])} failures={len(s['fail'])} verdict_flips={len(s['flips'])}")
        if s["diff"]:
            print(f"   |p_fake diff| max={max(s['diff']):.6f} mean={statistics.mean(s['diff']):.6f} "
                  f"(flask rounds p_fake to 4 dp -> floor 5e-5)")
            print(f"   latency flask p50={pct(s['t_flask'],50)*1000:.0f}ms p95={pct(s['t_flask'],95)*1000:.0f}ms | "
                  f"ovms(client incl. preprocessing) p50={pct(s['t_ovms'],50)*1000:.0f}ms p95={pct(s['t_ovms'],95)*1000:.0f}ms")
        for rel, ref, got in s["flips"]:
            print(f"   FLIP {rel}: flask={ref} ovms={got}")
        for rel, e in s["fail"]:
            print(f"   FAIL {rel}: {e}")
    print(f"\nper-clip CSV: {out}")


if __name__ == "__main__":
    main()
