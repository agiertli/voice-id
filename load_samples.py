#!/usr/bin/env python3
"""
Load audio samples into SQLite from the samples/ directory structure:

  samples/
    ai/
      en/
        fish-anton-hello.wav
        xtts-anton-greeting.wav
      cs/
        fish-anton-pozdrav.wav
      sk/
        fish-anton-ahoj.wav
    natural/
      en/
        anton-hello.wav
      cs/
        anton-pozdrav.wav
        katka-pozdrav.wav
      sk/
        anton-ahoj.wav

Filename convention (optional but recommended):
  For AI:      {model}-{speaker}-{description}.{ext}
  For natural: {speaker}-{description}.{ext}

The script extracts speaker and source from the filename when possible.
"""

import mimetypes
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "data/voiceid.db")
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "samples")

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".webm"}

AI_MODELS = {"fish", "xtts", "moss", "qwen", "higgs", "coqui"}


def parse_filename(filename, is_ai):
    stem = Path(filename).stem
    parts = stem.split("-", 2)

    source = ""
    speaker = ""
    if is_ai and len(parts) >= 2:
        if parts[0].lower() in AI_MODELS:
            source = parts[0]
            speaker = parts[1] if len(parts) > 1 else ""
        else:
            source = "unknown"
            speaker = parts[0]
    elif not is_ai and len(parts) >= 1:
        speaker = parts[0]

    return source, speaker


def get_content_type(filepath):
    mt, _ = mimetypes.guess_type(str(filepath))
    return mt or "audio/wav"


def load_samples():
    samples_dir = Path(SAMPLES_DIR)
    if not samples_dir.exists():
        print(f"Error: {samples_dir} does not exist")
        print(f"Create it with: mkdir -p {samples_dir}/ai/en {samples_dir}/natural/en")
        sys.exit(1)

    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clips (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            is_ai INTEGER NOT NULL,
            language TEXT NOT NULL,
            source TEXT DEFAULT '',
            speaker TEXT DEFAULT '',
            audio_data BLOB NOT NULL,
            content_type TEXT DEFAULT 'audio/wav',
            created_at REAL NOT NULL
        );
    """)

    existing = {row[0] for row in conn.execute("SELECT filename FROM clips").fetchall()}

    added = 0
    skipped = 0

    for category in ["ai", "natural"]:
        is_ai = category == "ai"
        cat_dir = samples_dir / category

        if not cat_dir.exists():
            print(f"  Skipping {cat_dir} (not found)")
            continue

        for lang_dir in sorted(cat_dir.iterdir()):
            if not lang_dir.is_dir():
                continue
            language = lang_dir.name

            for audio_file in sorted(lang_dir.iterdir()):
                if audio_file.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue

                rel_path = str(audio_file.relative_to(samples_dir))

                if rel_path in existing:
                    skipped += 1
                    continue

                source, speaker = parse_filename(audio_file.name, is_ai)
                content_type = get_content_type(audio_file)

                with open(audio_file, "rb") as f:
                    audio_data = f.read()

                clip_id = str(uuid.uuid4())[:8]
                conn.execute(
                    "INSERT INTO clips (id, filename, is_ai, language, source, speaker, audio_data, content_type, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (clip_id, rel_path, int(is_ai), language, source, speaker, audio_data, content_type, time.time()),
                )
                added += 1
                label = "AI" if is_ai else "HUMAN"
                print(f"  [{label}] {rel_path} -> {clip_id} (lang={language}, source={source}, speaker={speaker})")

    conn.commit()
    conn.close()

    total = added + skipped
    print(f"\nDone: {added} added, {skipped} skipped (already loaded), {total} total in DB")


if __name__ == "__main__":
    print(f"Loading samples from {SAMPLES_DIR} into {DB_PATH}\n")
    load_samples()
