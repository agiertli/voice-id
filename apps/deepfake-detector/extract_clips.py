"""Extract labeled audio clips from the Voice ID SQLite database for training."""

import os
import sqlite3
import subprocess
import sys

DB_PATH = os.environ.get("VOICEID_DB", "/tmp/test-voiceid-data2/voiceid.db")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")


def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        print("Set VOICEID_DB env var to the correct path")
        sys.exit(1)

    os.makedirs(os.path.join(OUTPUT_DIR, "real"), exist_ok=True)
    os.makedirs(os.path.join(OUTPUT_DIR, "fake"), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    clips = conn.execute(
        "SELECT id, filename, is_ai, source, speaker, language, audio_data FROM clips"
    ).fetchall()

    print(f"Found {len(clips)} clips")

    for clip in clips:
        label = "fake" if clip["is_ai"] else "real"
        clip_id = clip["id"][:8]
        src = clip["source"] or clip["speaker"] or "unknown"
        lang = clip["language"]

        raw_ext = clip["filename"].rsplit(".", 1)[-1] if clip["filename"] else "wav"
        raw_path = os.path.join(OUTPUT_DIR, label, f"{src}_{lang}_{clip_id}.{raw_ext}")

        with open(raw_path, "wb") as f:
            f.write(clip["audio_data"])

        # Convert to 16kHz mono WAV for training
        wav_path = raw_path.rsplit(".", 1)[0] + ".wav"
        if raw_ext != "wav" or True:  # always normalize
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_path, "-ac", "1", "-ar", "16000", "-f", "wav", wav_path],
                capture_output=True,
            )
            if raw_ext != "wav":
                os.remove(raw_path)

        print(f"  {label}/{os.path.basename(wav_path)} ({lang})")

    conn.close()
    print(f"\nClips saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
