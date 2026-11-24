"""
XTTS Regression Investigation (Section 14)

Investigates why the heavy (fine-tuned) model regresses on Jakub XTTS clips
compared to the base model. Runs both models on all Jakub clips, extracts
shortcut features, and analyzes the Miska false positive.

All Jakub clips come from apps/voice-id/samples/ai/ — they are ALL fake (AI-generated).
"""

import os
import json
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import librosa
import torch
from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_RATE = 16000
MAX_LENGTH = SAMPLE_RATE * 10  # 160000

BASE_MODEL = "garystafford/wav2vec2-deepfake-voice-detector"
HEAVY_MODEL = os.path.join(BASE_DIR, "models", "wav2vec2-heavy")
MANIFEST_PATH = os.path.join(BASE_DIR, "experiment_manifest.json")

# Jakub clips (all AI-generated = label fake)
JAKUB_DIR = "/Users/agiertli/Documents/work/voice-id/apps/voice-id/samples/ai"

# Miska false positive
MISKA_FP_PATH = os.path.join(
    BASE_DIR, "data_normalized", "real", "miska_cs_fa779860.wav"
)

# ---------------------------------------------------------------------------
# Unified preprocessing (from experiment_v2.py)
# ---------------------------------------------------------------------------

def preprocess_audio(audio, sample_rate=SAMPLE_RATE, max_length=MAX_LENGTH):
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)
    if len(audio) > max_length:
        audio = audio[:max_length]
    elif len(audio) < max_length:
        audio = np.pad(audio, (0, max_length - len(audio)))
    return audio


def extract_features_for_model(audio, feature_extractor):
    inputs = feature_extractor(
        audio,
        sampling_rate=SAMPLE_RATE,
        return_tensors="pt",
        padding="max_length",
        max_length=MAX_LENGTH,
        truncation=True,
    )
    return inputs


# ---------------------------------------------------------------------------
# Shortcut feature extraction (from experiment_v2.py)
# ---------------------------------------------------------------------------

def compute_silence_features(audio, sr=SAMPLE_RATE, silence_thresh_db=-40):
    frame_length = int(0.025 * sr)
    hop_length = int(0.010 * sr)

    frames = librosa.util.frame(audio, frame_length=frame_length, hop_length=hop_length)
    frame_energy = np.sum(frames ** 2, axis=0)
    frame_energy_db = 10 * np.log10(frame_energy + 1e-10)

    is_speech = frame_energy_db > silence_thresh_db
    n_frames = len(is_speech)

    if n_frames == 0:
        return {
            "vad_ratio": 0, "silence_ratio": 1,
            "n_speech_segments": 0, "n_pauses": 0,
            "mean_pause_dur": 0, "max_pause_dur": 0,
            "leading_silence": 0, "trailing_silence": 0,
        }

    vad_ratio = np.mean(is_speech)

    changes = np.diff(is_speech.astype(int))
    seg_starts = np.where(changes == 1)[0] + 1
    seg_ends = np.where(changes == -1)[0] + 1

    if is_speech[0]:
        seg_starts = np.concatenate([[0], seg_starts])
    if is_speech[-1]:
        seg_ends = np.concatenate([seg_ends, [n_frames]])

    n_speech_segments = len(seg_starts)

    pause_durs = []
    for i in range(len(seg_ends) - 1):
        if i < len(seg_starts) - 1:
            pause_frames = seg_starts[i + 1] - seg_ends[i]
            if pause_frames > 0:
                pause_durs.append(pause_frames * hop_length / sr)

    first_speech = seg_starts[0] if len(seg_starts) > 0 else n_frames
    last_speech = seg_ends[-1] if len(seg_ends) > 0 else 0
    leading_silence = first_speech * hop_length / sr
    trailing_silence = (n_frames - last_speech) * hop_length / sr

    return {
        "vad_ratio": float(vad_ratio),
        "silence_ratio": float(1 - vad_ratio),
        "n_speech_segments": n_speech_segments,
        "n_pauses": len(pause_durs),
        "mean_pause_dur": float(np.mean(pause_durs)) if pause_durs else 0,
        "max_pause_dur": float(np.max(pause_durs)) if pause_durs else 0,
        "leading_silence": float(leading_silence),
        "trailing_silence": float(trailing_silence),
    }


def compute_spectral_features(audio, sr=SAMPLE_RATE):
    spectral_centroid = librosa.feature.spectral_centroid(y=audio, sr=sr)[0]
    zcr = librosa.feature.zero_crossing_rate(audio)[0]
    spectral_flatness = librosa.feature.spectral_flatness(y=audio)[0]

    return {
        "spectral_centroid_mean": float(np.mean(spectral_centroid)),
        "zcr_mean": float(np.mean(zcr)),
        "spectral_flatness_mean": float(np.mean(spectral_flatness)),
    }


def compute_basic_features(audio, sr=SAMPLE_RATE):
    nonzero_end = len(audio)
    for i in range(len(audio) - 1, -1, -1):
        if abs(audio[i]) > 1e-8:
            nonzero_end = i + 1
            break
    actual_audio = audio[:nonzero_end]
    actual_duration = len(actual_audio) / sr
    rms = float(np.sqrt(np.mean(actual_audio ** 2))) if len(actual_audio) > 0 else 0
    peak = float(np.max(np.abs(actual_audio))) if len(actual_audio) > 0 else 0
    return {
        "duration": actual_duration,
        "rms": rms,
        "peak_amplitude": peak,
    }


def extract_all_shortcut_features(path):
    audio, sr = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    feats = {}
    feats.update(compute_basic_features(audio, sr))
    feats.update(compute_silence_features(audio, sr))
    feats.update(compute_spectral_features(audio, sr))
    return feats


# ---------------------------------------------------------------------------
# Model inference
# ---------------------------------------------------------------------------

def predict_clip(model, feature_extractor, path, device):
    audio, sr = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    audio = preprocess_audio(audio)
    inputs = extract_features_for_model(audio, feature_extractor)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)
        p_fake = probs[0][1].item()
    return p_fake


# ---------------------------------------------------------------------------
# Collect Jakub clips
# ---------------------------------------------------------------------------

def find_jakub_clips():
    clips = []
    for root, dirs, files in os.walk(JAKUB_DIR):
        for f in sorted(files):
            if "jakub" in f.lower() and f.endswith(".wav"):
                path = os.path.join(root, f)
                # Parse source model and language from path
                parts = root.split(os.sep)
                lang = parts[-1]  # en, cs, sk
                # source from filename: fish-jakub-xxx or xtts-jakub-xxx
                source = f.split("-")[0].upper()
                if source == "FISH":
                    source = "Fish"
                elif source == "XTTS":
                    source = "XTTS"
                clips.append({
                    "path": path,
                    "filename": f,
                    "source": source,
                    "language": lang,
                    "label": 1,  # all AI-generated = fake
                })
    return clips


# ---------------------------------------------------------------------------
# Main investigation
# ---------------------------------------------------------------------------

def main():
    device = torch.device("cpu")
    print("=" * 80)
    print("XTTS REGRESSION INVESTIGATION — Section 14")
    print("=" * 80)
    print(f"Device: {device}")

    # 1. Find Jakub clips
    jakub_clips = find_jakub_clips()
    print(f"\nFound {len(jakub_clips)} Jakub clips:")
    xtts_clips = [c for c in jakub_clips if c["source"] == "XTTS"]
    fish_clips = [c for c in jakub_clips if c["source"] == "Fish"]
    print(f"  XTTS: {len(xtts_clips)} clips")
    print(f"  Fish: {len(fish_clips)} clips")

    for c in jakub_clips:
        print(f"    {c['source']:5s} {c['language']:2s}  {c['filename']}")

    # 2. Load models
    print("\nLoading feature extractor from BASE model...")
    feature_extractor = AutoFeatureExtractor.from_pretrained(BASE_MODEL)

    print("Loading BASE model...")
    base_model = AutoModelForAudioClassification.from_pretrained(BASE_MODEL)
    base_model.to(device)
    base_model.eval()

    print("Loading HEAVY model...")
    heavy_model = AutoModelForAudioClassification.from_pretrained(HEAVY_MODEL)
    heavy_model.to(device)
    heavy_model.eval()

    # 3. Run both models on each Jakub clip
    print("\n" + "=" * 80)
    print("PART 1: BASE vs HEAVY MODEL — ALL JAKUB CLIPS")
    print("=" * 80)
    print(f"\n{'Filename':45s} {'Source':6s} {'Lang':4s} {'Base p_fake':>11s} {'Heavy p_fake':>12s} {'Base pred':>10s} {'Heavy pred':>11s} {'Shift':>8s}")
    print("-" * 105)

    results = []
    for clip in jakub_clips:
        base_pf = predict_clip(base_model, feature_extractor, clip["path"], device)
        heavy_pf = predict_clip(heavy_model, feature_extractor, clip["path"], device)
        base_pred = "FAKE" if base_pf >= 0.5 else "REAL"
        heavy_pred = "FAKE" if heavy_pf >= 0.5 else "REAL"
        shift = heavy_pf - base_pf

        # Correct prediction for fake clips: FAKE
        base_correct = base_pred == "FAKE"
        heavy_correct = heavy_pred == "FAKE"

        results.append({
            "filename": clip["filename"],
            "source": clip["source"],
            "language": clip["language"],
            "base_pfake": base_pf,
            "heavy_pfake": heavy_pf,
            "base_pred": base_pred,
            "heavy_pred": heavy_pred,
            "base_correct": base_correct,
            "heavy_correct": heavy_correct,
            "shift": shift,
        })

        base_mark = "+" if base_correct else "X"
        heavy_mark = "+" if heavy_correct else "X"
        print(f"  {clip['filename']:43s} {clip['source']:6s} {clip['language']:4s} "
              f"{base_pf:11.4f} {heavy_pf:12.4f} "
              f"{base_mark}{base_pred:>9s} {heavy_mark}{heavy_pred:>10s} {shift:+8.4f}")

    # Summary by source
    print("\n--- Summary by source ---")
    for src in ["XTTS", "Fish"]:
        src_results = [r for r in results if r["source"] == src]
        base_correct = sum(1 for r in src_results if r["base_correct"])
        heavy_correct = sum(1 for r in src_results if r["heavy_correct"])
        total = len(src_results)
        base_mean_pf = np.mean([r["base_pfake"] for r in src_results])
        heavy_mean_pf = np.mean([r["heavy_pfake"] for r in src_results])
        print(f"  {src:6s}: Base {base_correct}/{total}={base_correct/total:.0%} (mean p_fake={base_mean_pf:.4f})"
              f"  Heavy {heavy_correct}/{total}={heavy_correct/total:.0%} (mean p_fake={heavy_mean_pf:.4f})")

    # 4. Identify clips where base RIGHT but heavy WRONG
    print("\n" + "=" * 80)
    print("PART 2: CLIPS WHERE BASE=CORRECT, HEAVY=WRONG (REGRESSION)")
    print("=" * 80)

    regressions = [r for r in results if r["base_correct"] and not r["heavy_correct"]]
    if regressions:
        print(f"\nFound {len(regressions)} regression clips:")
        for r in regressions:
            print(f"  {r['filename']:43s} {r['source']:6s} {r['language']:4s}")
            print(f"    Base:  p_fake={r['base_pfake']:.4f} -> {r['base_pred']} (CORRECT)")
            print(f"    Heavy: p_fake={r['heavy_pfake']:.4f} -> {r['heavy_pred']} (WRONG)")
            print(f"    Shift: {r['shift']:+.4f}")
    else:
        print("\nNo regressions found (heavy got everything base got right)")

    # Also: clips where heavy=CORRECT but base=WRONG (improvements)
    improvements = [r for r in results if not r["base_correct"] and r["heavy_correct"]]
    if improvements:
        print(f"\nFound {len(improvements)} improvement clips (heavy fixed base's errors):")
        for r in improvements:
            print(f"  {r['filename']:43s} {r['source']:6s} {r['language']:4s}")
            print(f"    Base:  p_fake={r['base_pfake']:.4f} -> {r['base_pred']} (WRONG)")
            print(f"    Heavy: p_fake={r['heavy_pfake']:.4f} -> {r['heavy_pred']} (CORRECT)")
            print(f"    Shift: {r['shift']:+.4f}")

    # 5. Shortcut features for ALL Jakub clips
    print("\n" + "=" * 80)
    print("PART 3: SHORTCUT FEATURES — JAKUB CLIPS")
    print("=" * 80)

    key_features = ["leading_silence", "trailing_silence", "vad_ratio", "zcr_mean",
                    "duration", "rms", "spectral_centroid_mean", "spectral_flatness_mean"]

    print(f"\n{'Filename':45s} {'Source':6s} {'lead_sil':>9s} {'trail_sil':>10s} {'vad_ratio':>10s} {'zcr_mean':>9s} {'duration':>9s}")
    print("-" * 100)

    jakub_features = []
    for clip in jakub_clips:
        feats = extract_all_shortcut_features(clip["path"])
        jakub_features.append(feats)
        print(f"  {clip['filename']:43s} {clip['source']:6s} "
              f"{feats['leading_silence']:9.4f} {feats['trailing_silence']:10.4f} "
              f"{feats['vad_ratio']:10.4f} {feats['zcr_mean']:9.4f} {feats['duration']:9.3f}")

    # Means by source
    print("\n--- Mean shortcut features by source ---")
    for src in ["XTTS", "Fish"]:
        src_feats = [f for f, c in zip(jakub_features, jakub_clips) if c["source"] == src]
        if not src_feats:
            continue
        print(f"  {src}:")
        for k in key_features:
            vals = [f[k] for f in src_feats]
            print(f"    {k:30s} mean={np.mean(vals):.4f}  std={np.std(vals):.4f}  "
                  f"min={np.min(vals):.4f}  max={np.max(vals):.4f}")

    # 6. Compare with training data shortcut features
    print("\n" + "=" * 80)
    print("PART 4: JAKUB vs TRAINING DATA SHORTCUT COMPARISON")
    print("=" * 80)

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    train_real_feats = []
    train_fake_feats = []
    train_xtts_feats = []
    train_fish_feats = []

    for clip in manifest["train"]:
        feats = extract_all_shortcut_features(clip["path"])
        if clip["label"] == 0:
            train_real_feats.append(feats)
        else:
            train_fake_feats.append(feats)
            if "XTTS" in clip.get("source", ""):
                train_xtts_feats.append(feats)
            elif "Fish" in clip.get("source", ""):
                train_fish_feats.append(feats)

    for clip in manifest["val"]:
        feats = extract_all_shortcut_features(clip["path"])
        if clip["label"] == 0:
            train_real_feats.append(feats)
        else:
            train_fake_feats.append(feats)
            if "XTTS" in clip.get("source", ""):
                train_xtts_feats.append(feats)
            elif "Fish" in clip.get("source", ""):
                train_fish_feats.append(feats)

    print(f"\nTraining+val data: {len(train_real_feats)} real, {len(train_fake_feats)} fake "
          f"({len(train_xtts_feats)} XTTS, {len(train_fish_feats)} Fish)")

    # Jakub XTTS features
    jakub_xtts_feats = [f for f, c in zip(jakub_features, jakub_clips) if c["source"] == "XTTS"]
    jakub_fish_feats = [f for f, c in zip(jakub_features, jakub_clips) if c["source"] == "Fish"]

    print(f"\n{'Feature':30s} {'Train Real':>12s} {'Train Fake':>12s} {'Train XTTS':>12s} "
          f"{'Jakub XTTS':>12s} {'Train Fish':>12s} {'Jakub Fish':>12s}")
    print("-" * 105)

    for k in key_features:
        tr_real = np.mean([f[k] for f in train_real_feats])
        tr_fake = np.mean([f[k] for f in train_fake_feats])
        tr_xtts = np.mean([f[k] for f in train_xtts_feats]) if train_xtts_feats else float('nan')
        jk_xtts = np.mean([f[k] for f in jakub_xtts_feats]) if jakub_xtts_feats else float('nan')
        tr_fish = np.mean([f[k] for f in train_fish_feats]) if train_fish_feats else float('nan')
        jk_fish = np.mean([f[k] for f in jakub_fish_feats]) if jakub_fish_feats else float('nan')
        print(f"  {k:28s} {tr_real:12.4f} {tr_fake:12.4f} {tr_xtts:12.4f} "
              f"{jk_xtts:12.4f} {tr_fish:12.4f} {jk_fish:12.4f}")

    # 7. Miska false positive analysis
    print("\n" + "=" * 80)
    print("PART 5: MISKA FALSE POSITIVE — miska_cs_fa779860.wav")
    print("=" * 80)

    if os.path.exists(MISKA_FP_PATH):
        miska_feats = extract_all_shortcut_features(MISKA_FP_PATH)
        miska_base_pf = predict_clip(base_model, feature_extractor, MISKA_FP_PATH, device)
        miska_heavy_pf = predict_clip(heavy_model, feature_extractor, MISKA_FP_PATH, device)

        print(f"\n  File: {MISKA_FP_PATH}")
        print(f"  True label: REAL")
        print(f"  Base model:  p_fake = {miska_base_pf:.4f} -> {'FAKE' if miska_base_pf >= 0.5 else 'REAL'}")
        print(f"  Heavy model: p_fake = {miska_heavy_pf:.4f} -> {'FAKE' if miska_heavy_pf >= 0.5 else 'REAL'}")

        print(f"\n  Shortcut features:")
        for k in sorted(miska_feats.keys()):
            print(f"    {k:35s} = {miska_feats[k]:.4f}")

        print(f"\n  Comparison with training means:")
        print(f"    {'Feature':30s} {'Miska FP':>10s} {'Train Real':>12s} {'Train Fake':>12s} {'Closer to':>12s}")
        print("    " + "-" * 75)
        for k in key_features:
            val = miska_feats[k]
            tr_real_mean = np.mean([f[k] for f in train_real_feats])
            tr_fake_mean = np.mean([f[k] for f in train_fake_feats])
            closer = "REAL" if abs(val - tr_real_mean) < abs(val - tr_fake_mean) else "FAKE"
            print(f"    {k:30s} {val:10.4f} {tr_real_mean:12.4f} {tr_fake_mean:12.4f} {closer:>12s}")

        # Also compare against other real clips
        print(f"\n  Miska FP leading_silence vs all real clips:")
        for clip in manifest["train"] + manifest["val"] + manifest["held_out"]:
            if clip["label"] == 0:
                cf = extract_all_shortcut_features(clip["path"])
                flag = " *** MISKA FP" if "fa779860" in clip["filename"] else ""
                print(f"    {clip['filename']:35s} leading_silence={cf['leading_silence']:.4f}  "
                      f"trailing_silence={cf['trailing_silence']:.4f}  vad_ratio={cf['vad_ratio']:.4f}{flag}")
    else:
        print(f"  File not found: {MISKA_FP_PATH}")

    # 8. Key finding: correlation between p_fake shift and features
    print("\n" + "=" * 80)
    print("PART 6: CORRELATION — p_fake SHIFT vs SHORTCUT FEATURES")
    print("=" * 80)

    print(f"\n{'Filename':45s} {'Source':6s} {'lead_sil':>9s} {'base_pf':>8s} {'heavy_pf':>9s} {'shift':>8s}")
    print("-" * 90)

    for r, feats in zip(results, jakub_features):
        print(f"  {r['filename']:43s} {r['source']:6s} "
              f"{feats['leading_silence']:9.4f} {r['base_pfake']:8.4f} "
              f"{r['heavy_pfake']:9.4f} {r['shift']:+8.4f}")

    # Correlation
    shifts = np.array([r["shift"] for r in results])
    lead_sils = np.array([f["leading_silence"] for f in jakub_features])
    if len(shifts) > 2:
        corr = np.corrcoef(lead_sils, shifts)[0, 1]
        print(f"\n  Pearson correlation(leading_silence, p_fake_shift): {corr:.4f}")

    base_pfs = np.array([r["base_pfake"] for r in results])
    heavy_pfs = np.array([r["heavy_pfake"] for r in results])
    corr_base = np.corrcoef(lead_sils, base_pfs)[0, 1] if len(base_pfs) > 2 else float('nan')
    corr_heavy = np.corrcoef(lead_sils, heavy_pfs)[0, 1] if len(heavy_pfs) > 2 else float('nan')
    print(f"  Pearson correlation(leading_silence, base_p_fake):  {corr_base:.4f}")
    print(f"  Pearson correlation(leading_silence, heavy_p_fake): {corr_heavy:.4f}")

    # 9. Final summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    xtts_results = [r for r in results if r["source"] == "XTTS"]
    fish_results = [r for r in results if r["source"] == "Fish"]

    base_xtts_correct = sum(1 for r in xtts_results if r["base_correct"])
    heavy_xtts_correct = sum(1 for r in xtts_results if r["heavy_correct"])
    base_fish_correct = sum(1 for r in fish_results if r["base_correct"])
    heavy_fish_correct = sum(1 for r in fish_results if r["heavy_correct"])

    print(f"\n  Jakub XTTS clips ({len(xtts_results)} total):")
    print(f"    Base:  {base_xtts_correct}/{len(xtts_results)} correct "
          f"(mean p_fake = {np.mean([r['base_pfake'] for r in xtts_results]):.4f})")
    print(f"    Heavy: {heavy_xtts_correct}/{len(xtts_results)} correct "
          f"(mean p_fake = {np.mean([r['heavy_pfake'] for r in xtts_results]):.4f})")

    print(f"\n  Jakub Fish clips ({len(fish_results)} total):")
    print(f"    Base:  {base_fish_correct}/{len(fish_results)} correct "
          f"(mean p_fake = {np.mean([r['base_pfake'] for r in fish_results]):.4f})")
    print(f"    Heavy: {heavy_fish_correct}/{len(fish_results)} correct "
          f"(mean p_fake = {np.mean([r['heavy_pfake'] for r in fish_results]):.4f})")

    # Leading silence distribution
    jakub_xtts_lead = [f["leading_silence"] for f, c in zip(jakub_features, jakub_clips) if c["source"] == "XTTS"]
    jakub_fish_lead = [f["leading_silence"] for f, c in zip(jakub_features, jakub_clips) if c["source"] == "Fish"]
    train_real_lead = [f["leading_silence"] for f in train_real_feats]
    train_fake_lead = [f["leading_silence"] for f in train_fake_feats]

    print(f"\n  Leading silence (key shortcut feature):")
    print(f"    Train REAL:    mean={np.mean(train_real_lead):.4f}s  (range {np.min(train_real_lead):.4f}-{np.max(train_real_lead):.4f})")
    print(f"    Train FAKE:    mean={np.mean(train_fake_lead):.4f}s  (range {np.min(train_fake_lead):.4f}-{np.max(train_fake_lead):.4f})")
    print(f"    Jakub XTTS:    mean={np.mean(jakub_xtts_lead):.4f}s  (range {np.min(jakub_xtts_lead):.4f}-{np.max(jakub_xtts_lead):.4f})")
    print(f"    Jakub Fish:    mean={np.mean(jakub_fish_lead):.4f}s  (range {np.min(jakub_fish_lead):.4f}-{np.max(jakub_fish_lead):.4f})")

    print("\nDone.")


if __name__ == "__main__":
    main()
