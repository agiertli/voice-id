"""
Step 2: Prepare experiment — normalize audio, isolate held-out set, create splits.

This script:
1. Normalizes ALL clips to 16kHz mono WAV (removes sample-rate shortcut)
2. Loudness-normalizes all clips to the same RMS (removes loudness shortcut)
3. Copies held-out clips to demo_test/ BEFORE any training
4. Creates train/val splits from remaining clips
5. Writes a manifest JSON that the training script reads

Run ONCE: python prepare_experiment.py
"""

import os
import glob
import json
import shutil
import subprocess
import sys

import numpy as np
import soundfile as sf
import librosa

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEMO_TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_test")
NORMALIZED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_normalized")
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiment_manifest.json")

TARGET_SR = 16000
TARGET_RMS = 0.05


def normalize_audio(input_path, output_path):
    audio, sr = librosa.load(input_path, sr=TARGET_SR, mono=True)

    current_rms = np.sqrt(np.mean(audio ** 2))
    if current_rms > 1e-6:
        audio = audio * (TARGET_RMS / current_rms)
    audio = np.clip(audio, -1.0, 1.0)

    sf.write(output_path, audio, TARGET_SR)
    return len(audio) / TARGET_SR


def parse_clip_metadata(filename, label_dir):
    """Extract source, language, speaker from filename."""
    name = filename.rsplit(".", 1)[0]
    parts = name.split("_")

    if label_dir == "fake":
        source = "_".join(parts[:-2]) if len(parts) > 2 else parts[0]
        lang = parts[-2] if len(parts) >= 3 else "?"
        clip_id = parts[-1]
    else:
        speaker = parts[0] if len(parts) >= 3 else "unknown"
        source = speaker
        lang = parts[-2] if len(parts) >= 3 else "?"
        clip_id = parts[-1] if len(parts) >= 2 else name

    return {
        "source": source if label_dir == "fake" else parts[0],
        "language": lang,
        "clip_id": clip_id,
        "label": 1 if label_dir == "fake" else 0,
        "label_name": label_dir,
    }


def main():
    clips = []
    for label_dir in ["real", "fake"]:
        src_dir = os.path.join(DATA_DIR, label_dir)
        wavs = sorted(glob.glob(os.path.join(src_dir, "*.wav")))
        for wav_path in wavs:
            fname = os.path.basename(wav_path)
            meta = parse_clip_metadata(fname, label_dir)
            meta["original_path"] = wav_path
            meta["filename"] = fname
            clips.append(meta)

    print(f"Found {len(clips)} clips")
    print(f"  Real: {sum(1 for c in clips if c['label'] == 0)}")
    print(f"  Fake: {sum(1 for c in clips if c['label'] == 1)}")

    # --- Step 1: Normalize all audio ---
    os.makedirs(os.path.join(NORMALIZED_DIR, "real"), exist_ok=True)
    os.makedirs(os.path.join(NORMALIZED_DIR, "fake"), exist_ok=True)

    for clip in clips:
        out_path = os.path.join(NORMALIZED_DIR, clip["label_name"], clip["filename"])
        dur = normalize_audio(clip["original_path"], out_path)
        clip["normalized_path"] = out_path
        clip["duration"] = dur

    # Verify normalization
    sample_rates = set()
    for clip in clips:
        audio, sr = librosa.load(clip["normalized_path"], sr=None, mono=True)
        sample_rates.add(sr)
        rms = np.sqrt(np.mean(audio ** 2))
        clip["normalized_rms"] = float(rms)

    print(f"\nAfter normalization:")
    print(f"  Sample rates: {sample_rates}")
    print(f"  RMS range: {min(c['normalized_rms'] for c in clips):.4f} - "
          f"{max(c['normalized_rms'] for c in clips):.4f}")

    # --- Step 2: Select held-out clips ---
    # Strategy: pick 2 clips per category (source×language) for held-out
    # These are NEVER seen during training or validation

    # Group clips by (label, source, language)
    groups = {}
    for clip in clips:
        key = (clip["label_name"], clip["source"], clip["language"])
        groups.setdefault(key, []).append(clip)

    print(f"\nClip groups:")
    for key, group_clips in sorted(groups.items()):
        print(f"  {key}: {len(group_clips)} clips")

    held_out = []
    remaining = []

    for key, group_clips in sorted(groups.items()):
        n_held = max(1, len(group_clips) // 4)
        n_held = min(n_held, 2)

        # Take last N clips (alphabetically) as held-out for reproducibility
        sorted_clips = sorted(group_clips, key=lambda c: c["filename"])
        held_out.extend(sorted_clips[-n_held:])
        remaining.extend(sorted_clips[:-n_held])

    print(f"\nHeld-out: {len(held_out)} clips")
    print(f"  Real: {sum(1 for c in held_out if c['label'] == 0)}")
    print(f"  Fake: {sum(1 for c in held_out if c['label'] == 1)}")
    print(f"Remaining for train/val: {len(remaining)} clips")

    # --- Step 3: Copy held-out to demo_test/ (LOCKED) ---
    if os.path.exists(DEMO_TEST_DIR):
        shutil.rmtree(DEMO_TEST_DIR)
    os.makedirs(os.path.join(DEMO_TEST_DIR, "real"), exist_ok=True)
    os.makedirs(os.path.join(DEMO_TEST_DIR, "fake"), exist_ok=True)

    for clip in held_out:
        src = clip["normalized_path"]
        dst = os.path.join(DEMO_TEST_DIR, clip["label_name"], clip["filename"])
        shutil.copy2(src, dst)
        clip["demo_test_path"] = dst
        print(f"  HELD OUT: {clip['label_name']}/{clip['filename']} "
              f"({clip['source']}, {clip['language']})")

    # --- Step 4: Split remaining into train/val ---
    # Use language-stratified split: ensure each language appears in both sets
    # But since we have few clips, use a simple stratified random split
    import random
    random.seed(42)

    # Shuffle remaining
    random.shuffle(remaining)

    # 75% train, 25% val — stratified by label
    real_remaining = [c for c in remaining if c["label"] == 0]
    fake_remaining = [c for c in remaining if c["label"] == 1]

    n_val_real = max(2, len(real_remaining) // 4)
    n_val_fake = max(3, len(fake_remaining) // 4)

    val_clips = real_remaining[:n_val_real] + fake_remaining[:n_val_fake]
    train_clips = real_remaining[n_val_real:] + fake_remaining[n_val_fake:]

    print(f"\nTrain: {len(train_clips)} ({sum(1 for c in train_clips if c['label']==0)} real, "
          f"{sum(1 for c in train_clips if c['label']==1)} fake)")
    print(f"Val: {len(val_clips)} ({sum(1 for c in val_clips if c['label']==0)} real, "
          f"{sum(1 for c in val_clips if c['label']==1)} fake)")
    print(f"Held-out: {len(held_out)} ({sum(1 for c in held_out if c['label']==0)} real, "
          f"{sum(1 for c in held_out if c['label']==1)} fake)")

    # Check language coverage
    for split_name, split_clips in [("train", train_clips), ("val", val_clips), ("held_out", held_out)]:
        langs = set(c["language"] for c in split_clips)
        sources = set(c["source"] for c in split_clips)
        print(f"  {split_name}: langs={langs}, sources={sources}")

    # --- Step 5: Write manifest ---
    def clip_record(c):
        return {
            "path": c["normalized_path"],
            "filename": c["filename"],
            "label": c["label"],
            "label_name": c["label_name"],
            "source": c["source"],
            "language": c["language"],
            "duration": c["duration"],
        }

    manifest = {
        "train": [clip_record(c) for c in train_clips],
        "val": [clip_record(c) for c in val_clips],
        "held_out": [clip_record(c) for c in held_out],
        "normalization": {
            "sample_rate": TARGET_SR,
            "target_rms": TARGET_RMS,
        },
    }

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nManifest written to {MANIFEST_PATH}")
    print(f"Demo test clips copied to {DEMO_TEST_DIR}/")
    print(f"\nDO NOT modify demo_test/ — these are the permanent held-out clips.")


if __name__ == "__main__":
    main()
