"""
In-cluster parity + benchmark: legacy detector /predict vs OVMS adapter /predict (same serve.py API).
Stdlib only. Feed it WAVs already converted the way the game does (ffmpeg mono 22050 Hz s16).

    python3 ovms_bench.py --wavs /data/wav \
        --pair gary-finetuned=http://deepfake-detector-fine-tuned.voice-id.svc:8080,http://adapter-gary-finetuned:8080 \
        --pair gary-base=http://deepfake-detector-base.voice-id.svc:8080,http://adapter-gary-base:8080 \
        [--phases parity,batch5,sustained] [--batches 20] [--duration 60] [--clients 10]

Phases:
  parity     every clip, sequentially, old then new: verdict flips, |dp_fake|, single-request latency
  batch5     N batches of 5 parallel requests (what the game does before a battle): wall time per batch
  sustained  C concurrent clients for D seconds: throughput, latency
Prints one JSON summary line per phase/model/backend prefixed with "RESULT ".
Audio is Rule Zero data: run in an ephemeral pod/emptyDir and delete it afterwards.
"""

import argparse
import json
import os
import random
import statistics
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor


def post(url, wav, timeout=300):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"{url}/predict", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    return out, time.perf_counter() - t


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


def emit(**kw):
    print("RESULT " + json.dumps(kw), flush=True)


def lat_stats(xs):
    return {"n": len(xs), "p50_ms": round(pct(xs, 50) * 1000), "p95_ms": round(pct(xs, 95) * 1000),
            "mean_ms": round(statistics.mean(xs) * 1000) if xs else None}


def parity(model, old, new, clips):
    flips, diffs, t_old, t_new, fails = [], [], [], [], []
    for name, wav in clips:
        try:
            a, ta = post(old, wav)
            b, tb = post(new, wav)
        except Exception as e:  # noqa: BLE001
            fails.append((name, repr(e)))
            continue
        t_old.append(ta); t_new.append(tb)
        diffs.append(abs(a["prob_fake"] - b["prob_fake"]))
        if a["verdict"] != b["verdict"] or set(a) != set(b):
            flips.append({"clip": name.split("__", 1)[0], "old": a, "new": b})
    emit(phase="parity", model=model, clips=len(clips), failures=len(fails), flips=len(flips),
         max_abs_dp=round(max(diffs), 6) if diffs else None,
         mean_abs_dp=round(statistics.mean(diffs), 6) if diffs else None,
         old=lat_stats(t_old), new=lat_stats(t_new), flip_detail=flips[:10],
         fail_detail=[(n.split("__", 1)[0], e) for n, e in fails[:10]])


def batch5(model, label, url, clips, batches, rng):
    walls, errs = [], 0
    with ThreadPoolExecutor(5) as ex:
        for _ in range(batches):
            pick = rng.sample(clips, 5)
            t = time.perf_counter()
            futs = [ex.submit(post, url, w) for _, w in pick]
            for f in futs:
                try:
                    f.result()
                except Exception:  # noqa: BLE001
                    errs += 1
            walls.append(time.perf_counter() - t)
    emit(phase="batch5", model=model, backend=label, batches=batches, errors=errs,
         wall_p50_ms=round(pct(walls, 50) * 1000), wall_p95_ms=round(pct(walls, 95) * 1000),
         wall_mean_ms=round(statistics.mean(walls) * 1000))


def sustained(model, label, url, clips, duration, clients, seed):
    stop = time.perf_counter() + duration
    lats, errs, lock = [], [0], threading.Lock()

    def worker(i):
        r = random.Random(seed + i)
        while time.perf_counter() < stop:
            _, w = r.choice(clips)
            try:
                _, t = post(url, w)
                with lock:
                    lats.append(t)
            except Exception:  # noqa: BLE001
                with lock:
                    errs[0] += 1

    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(clients)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    el = time.perf_counter() - t0
    emit(phase="sustained", model=model, backend=label, clients=clients, seconds=round(el, 1),
         completed=len(lats), errors=errs[0], rps=round(len(lats) / el, 3), **lat_stats(lats))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wavs", required=True)
    ap.add_argument("--pair", action="append", required=True, help="model=old_url,new_url")
    ap.add_argument("--phases", default="parity,batch5,sustained")
    ap.add_argument("--backends", default="old,new", help="for batch5/sustained")
    ap.add_argument("--batches", type=int, default=20)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--clients", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    clips = [(f, open(os.path.join(a.wavs, f), "rb").read()) for f in sorted(os.listdir(a.wavs)) if f.endswith(".wav")]
    print(f"{len(clips)} clips loaded", flush=True)
    phases = a.phases.split(",")
    for p in a.pair:
        model, urls = p.split("=", 1)
        old, new = urls.split(",")
        if "parity" in phases:
            parity(model, old, new, clips)
        for label, url in (("old", old), ("new", new)):
            if label not in a.backends.split(","):
                continue
            if "batch5" in phases:
                batch5(model, label, url, clips, a.batches, random.Random(a.seed))
            if "sustained" in phases:
                time.sleep(5)
                print(f"SUSTAINED-START {model} {label} {time.time():.0f}", flush=True)
                sustained(model, label, url, clips, a.duration, a.clients, a.seed)
                print(f"SUSTAINED-END {model} {label} {time.time():.0f}", flush=True)


if __name__ == "__main__":
    main()
