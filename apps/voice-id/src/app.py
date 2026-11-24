import base64
import csv
import functools
import hashlib
import io
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
import warnings
import zipfile
from pathlib import Path
from urllib.parse import quote

import imageio_ffmpeg
from flask import Flask, Response, g, jsonify, render_template, request, send_file

try:
    import numpy as np
    from scipy.io import wavfile as scipy_wavfile
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_SPECTROGRAM = True
except ImportError:
    HAS_SPECTROGRAM = False

try:
    # Pillow is not pinned in requirements.txt but is a hard dependency of matplotlib,
    # so it is present in the image. Optional: photos are stored as-is without it.
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import requests as http_requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
GAME_SIZE = int(os.environ.get("GAME_SIZE", "5"))
PRIZE_THRESHOLD = int(os.environ.get("PRIZE_THRESHOLD", "4"))
CONFERENCE_NAME = os.environ.get("CONFERENCE_NAME", "Red Hat Summit")
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "en")
AI_JUDGE_URL = os.environ.get("AI_JUDGE_URL", "")
AI_JUDGE_MODEL = os.environ.get("AI_JUDGE_MODEL", "gemma-4-12b")
AI_JUDGE2_URL = os.environ.get("AI_JUDGE2_URL", "")
AI_JUDGE2_MODEL = os.environ.get("AI_JUDGE2_MODEL", "Qwen2-Audio-7B-Instruct")
DETECTOR_URL = os.environ.get("DETECTOR_URL", "")
DETECTOR_BASE_URL = os.environ.get("DETECTOR_BASE_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "v01c3@r3dh4t!")


def _ensure_wav(audio_data, content_type):
    """Transcode non-WAV audio to WAV for universal browser compatibility."""
    if content_type in ("audio/wav", "audio/x-wav", "audio/wave"):
        return audio_data, "audio/wav"
    if audio_data[:4] == b"RIFF":
        return audio_data, "audio/wav"
    try:
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg_path = "ffmpeg"
    with tempfile.NamedTemporaryFile(suffix=".input", delete=False) as inp:
        inp.write(audio_data)
        inp_path = inp.name
    out_path = inp_path + ".wav"
    try:
        subprocess.run(
            [ffmpeg_path, "-y", "-i", inp_path, "-ar", "44100", "-ac", "1", out_path],
            capture_output=True, check=True, timeout=30,
        )
        with open(out_path, "rb") as f:
            return f.read(), "audio/wav"
    except Exception as e:
        logger.warning("Transcode to WAV failed: %s", e)
        return audio_data, content_type
    finally:
        for p in (inp_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def load_i18n():
    i18n_path = CONFIG_DIR / "i18n.json"
    if i18n_path.exists():
        with open(i18n_path) as f:
            return json.load(f)
    return {"en": {}}


def get_db():
    if "db" not in g:
        db_path = DATA_DIR / "voiceid.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(str(db_path))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db_path = DATA_DIR / "voiceid.db"
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

        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            clip_ids TEXT NOT NULL,
            guesses TEXT DEFAULT '{}',
            score INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            duration_sec REAL DEFAULT 0,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leaderboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name TEXT NOT NULL,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            duration_sec REAL,
            game_id TEXT,
            created_at REAL NOT NULL
        );
    """)
    for col, default in [("points", 0), ("duration_sec", 0)]:
        try:
            conn.execute(f"ALTER TABLE games ADD COLUMN {col} {'INTEGER' if col == 'points' else 'REAL'} DEFAULT {default}")
        except Exception:
            pass
    for col, col_type, default in [("points", "INTEGER", "0"), ("game_id", "TEXT", "NULL"), ("opponent", "TEXT", "NULL")]:
        try:
            conn.execute(f"ALTER TABLE leaderboard ADD COLUMN {col} {col_type} DEFAULT {default}")
        except Exception:
            pass
    for col, col_type, default in [("opponent", "TEXT", "NULL")]:
        try:
            conn.execute(f"ALTER TABLE games ADD COLUMN {col} {col_type} DEFAULT {default}")
        except Exception:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_stats (
            clip_id TEXT PRIMARY KEY,
            times_played INTEGER DEFAULT 0,
            times_guessed_ai INTEGER DEFAULT 0,
            times_guessed_human INTEGER DEFAULT 0
        )
    """)
    # Speaker profiles (display name + optional photo) for personal reveals.
    # name = lowercased clips.speaker key. Additive table -> safe on existing DBs.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS speakers (
            name TEXT PRIMARY KEY,
            display_name TEXT,
            photo BLOB,
            photo_type TEXT,
            updated_at REAL
        )
    """)
    conn.commit()
    conn.close()


def require_admin(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == ADMIN_TOKEN:
            return f(*args, **kwargs)
        return jsonify({"error": "Forbidden"}), 403
    return wrapper


# --- Pages ---

@app.route("/")
def index():
    return render_template("index.html",
                           conference_name=CONFERENCE_NAME,
                           game_size=GAME_SIZE,
                           prize_threshold=PRIZE_THRESHOLD,
                           default_lang=DEFAULT_LANG)


@app.route("/api/i18n")
def api_i18n():
    return jsonify(load_i18n())


@app.route("/tv")
def tv():
    return render_template("tv.html", conference_name=CONFERENCE_NAME)


@app.route("/admin")
def admin():
    return render_template("admin.html", conference_name=CONFERENCE_NAME)


# --- Audio serving ---

@app.route("/api/clip/<clip_id>/audio")
def serve_clip_audio(clip_id):
    db = get_db()
    row = db.execute(
        "SELECT audio_data, content_type, filename, is_ai, language, source, speaker FROM clips WHERE id = ?",
        (clip_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "clip not found"}), 404
    ai_label = "AI" if row["is_ai"] else "HUMAN"
    audio_data = row["audio_data"]
    orig_ct = row["content_type"]
    audio_data, content_type = _ensure_wav(audio_data, orig_ct)
    if orig_ct != content_type:
        db.execute("UPDATE clips SET audio_data = ?, content_type = ? WHERE id = ?",
                   (audio_data, content_type, clip_id))
        db.commit()
        logger.info("CLIP_TRANSCODED id=%s from=%s", clip_id, orig_ct)
    logger.info("CLIP_PLAY id=%s file=%s type=%s lang=%s source=%s speaker=%s size=%d",
                clip_id, row["filename"], ai_label, row["language"],
                row["source"] or "-", row["speaker"] or "-", len(audio_data))
    return Response(audio_data, mimetype=content_type,
                    headers={"Content-Length": str(len(audio_data))})


# --- Game API ---

@app.route("/api/player/lookup")
def player_lookup():
    nickname = request.args.get("nickname", "").strip()
    if not nickname:
        return jsonify({"found": False})
    db = get_db()
    player = db.execute("SELECT nickname, email FROM players WHERE nickname = ?", (nickname,)).fetchone()
    if not player:
        return jsonify({"found": False})
    best = db.execute(
        "SELECT MAX(score) as best, MAX(points) as best_points, COUNT(*) as games FROM leaderboard WHERE player_name = ?",
        (nickname,),
    ).fetchone()
    return jsonify({
        "found": True, "nickname": player["nickname"], "email": player["email"],
        "best_score": best["best"] if best else None,
        "best_points": best["best_points"] if best else 0,
        "games_played": best["games"] if best else 0,
    })


@app.route("/api/game/start", methods=["POST"])
def start_game():
    data = request.get_json(force=True)
    player_name = data.get("player_name", "").strip()
    email = data.get("email", "").strip()
    if not player_name:
        return jsonify({"error": "player_name required"}), 400

    db = get_db()
    existing = db.execute("SELECT id, email FROM players WHERE nickname = ?", (player_name,)).fetchone()
    if existing:
        if email and email != existing["email"]:
            db.execute("UPDATE players SET email = ? WHERE id = ?", (email, existing["id"]))
            db.commit()
    else:
        db.execute("INSERT INTO players (nickname, email, created_at) VALUES (?, ?, ?)",
                   (player_name, email, time.time()))
        db.commit()
    ai_clips = [dict(c) for c in db.execute("SELECT id, language FROM clips WHERE is_ai = 1").fetchall()]
    human_clips = [dict(c) for c in db.execute("SELECT id, language FROM clips WHERE is_ai = 0").fetchall()]

    if len(ai_clips) < 1 or len(human_clips) < 1:
        return jsonify({"error": "Need at least 1 AI and 1 human clip"}), 500
    if len(ai_clips) + len(human_clips) < GAME_SIZE:
        return jsonify({"error": f"Not enough clips ({len(ai_clips) + len(human_clips)}/{GAME_SIZE})"}), 500

    max_ai = min(GAME_SIZE - 1, len(ai_clips))
    min_ai = max(1, GAME_SIZE - len(human_clips))
    num_ai = random.randint(min_ai, max_ai)
    num_human = GAME_SIZE - num_ai

    selected = random.sample(ai_clips, num_ai) + random.sample(human_clips, num_human)
    random.shuffle(selected)
    clip_ids = [c["id"] for c in selected]

    game_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO games (id, player_name, clip_ids, created_at) VALUES (?, ?, ?, ?)",
        (game_id, player_name, json.dumps(clip_ids), time.time()),
    )
    db.commit()

    clip_details = []
    for c in selected:
        row = db.execute("SELECT filename, is_ai, source, speaker FROM clips WHERE id = ?", (c["id"],)).fetchone()
        ai_label = "AI" if row["is_ai"] else "HUMAN"
        clip_details.append(f'{c["id"]}({ai_label}/{c["language"]}/{row["filename"]})')
    logger.info("GAME_START game=%s player=%s clips=[%s]", game_id, player_name, ", ".join(clip_details))

    return jsonify({
        "game_id": game_id,
        "clips": [{"id": c["id"], "language": c["language"]} for c in selected],
    })


@app.route("/api/game/<game_id>/guess", methods=["POST"])
def submit_guess(game_id):
    data = request.get_json(force=True)
    clip_id = data.get("clip_id")
    guess = data.get("guess")

    if not clip_id or guess not in ("ai", "human"):
        return jsonify({"error": "clip_id and guess (ai|human) required"}), 400

    db = get_db()
    clip = db.execute("SELECT is_ai, source, speaker, language FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not clip:
        return jsonify({"error": "unknown clip_id"}), 404

    actual_is_ai = bool(clip["is_ai"])
    correct = (guess == "ai") == actual_is_ai
    actual_label = "ai" if actual_is_ai else "human"

    clip_row = db.execute("SELECT filename FROM clips WHERE id = ?", (clip_id,)).fetchone()
    logger.info("GUESS game=%s clip=%s file=%s guess=%s actual=%s correct=%s",
                game_id, clip_id, clip_row["filename"], guess, actual_label, correct)

    row = db.execute("SELECT guesses FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row:
        return jsonify({"error": "unknown game_id"}), 404

    guesses = json.loads(row["guesses"])
    guesses[clip_id] = {"guess": guess, "correct": correct}
    db.execute("UPDATE games SET guesses = ? WHERE id = ?", (json.dumps(guesses), game_id))

    ai_col = "times_guessed_ai" if guess == "ai" else "times_guessed_human"
    db.execute(
        f"INSERT INTO clip_stats (clip_id, times_played, {ai_col}) VALUES (?, 1, 1) "
        f"ON CONFLICT(clip_id) DO UPDATE SET times_played = times_played + 1, {ai_col} = {ai_col} + 1",
        (clip_id,),
    )
    db.commit()

    reveal = _speaker_reveal(db, clip["speaker"])

    return jsonify({
        "correct": correct,
        "actual": "ai" if actual_is_ai else "human",
        "source": clip["source"] or "",
        "speaker": clip["speaker"] or "",
        "language": clip["language"],
        "speaker_display": reveal["speaker_display"],
        "speaker_photo": reveal["speaker_photo"],
        "speaker_is_real_person": reveal["speaker_is_real_person"],
    })


@app.route("/api/game/<game_id>/complete", methods=["POST"])
def complete_game(game_id):
    db = get_db()
    row = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row:
        return jsonify({"error": "unknown game_id"}), 404

    data = request.get_json(silent=True) or {}

    guesses = json.loads(row["guesses"])
    score = sum(1 for g in guesses.values() if g["correct"])
    total = len(guesses)
    points = int(data.get("points", 0))
    duration = float(data.get("duration_sec", 0)) or (time.time() - row["created_at"])
    opponent = data.get("opponent") or None

    db.execute(
        "UPDATE games SET score = ?, total = ?, completed = 1, points = ?, duration_sec = ?, opponent = ? WHERE id = ?",
        (score, total, points, duration, opponent, game_id),
    )
    db.execute(
        "INSERT INTO leaderboard (player_name, score, total, points, duration_sec, game_id, opponent, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row["player_name"], score, total, points, duration, game_id, opponent, time.time()),
    )
    db.commit()

    # Rank among players by their BEST game (same basis as /api/leaderboard and
    # total_players), so "#rank of N players" is consistent. Ties share a rank.
    best_row = db.execute(
        "SELECT MAX(points) AS best FROM leaderboard WHERE player_name = ?",
        (row["player_name"],),
    ).fetchone()
    best = max(points, best_row["best"] or 0)
    rank_row = db.execute(
        "SELECT COUNT(*) AS rank FROM ("
        "  SELECT player_name, MAX(points) AS best FROM leaderboard"
        "  GROUP BY player_name"
        ") WHERE best > ?",
        (best,),
    ).fetchone()
    rank = rank_row["rank"] + 1

    return jsonify({
        "score": score,
        "total": total,
        "points": points,
        "duration_sec": round(duration, 1),
        "rank": rank,
        "prize": score >= PRIZE_THRESHOLD,
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    db = get_db()
    rows = db.execute("""
        SELECT player_name, score, total, points, duration_sec, opponent, created_at
        FROM leaderboard l1
        WHERE l1.id = (
            SELECT l2.id FROM leaderboard l2
            WHERE l2.player_name = l1.player_name
            ORDER BY l2.points DESC, l2.score DESC
            LIMIT 1
        )
        ORDER BY points DESC, score DESC
        LIMIT 20
    """).fetchall()
    total_players = db.execute(
        "SELECT COUNT(DISTINCT player_name) as cnt FROM leaderboard"
    ).fetchone()["cnt"]
    return jsonify({
        "leaderboard": [dict(r) for r in rows],
        "total_players": total_players,
    })


@app.route("/api/admin/clips")
def admin_clips():
    db = get_db()
    rows = db.execute(
        "SELECT id, filename, is_ai, language, source, speaker, content_type, created_at FROM clips"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/upload", methods=["POST"])
def admin_upload_clip():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "audio file required"}), 400

    is_ai = request.form.get("is_ai", "0")
    language = request.form.get("language", "en")
    source = request.form.get("source", "")
    speaker = request.form.get("speaker", "")
    filename = request.form.get("filename", audio_file.filename or "upload.wav")

    raw_content_type = audio_file.content_type or "audio/wav"
    audio_data = audio_file.read()
    audio_data, content_type = _ensure_wav(audio_data, raw_content_type)

    clip_id = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute(
        "INSERT INTO clips (id, filename, is_ai, language, source, speaker, audio_data, content_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clip_id, filename, int(is_ai), language, source, speaker, audio_data, content_type, time.time()),
    )
    db.commit()

    return jsonify({"id": clip_id, "filename": filename, "is_ai": bool(int(is_ai)), "language": language})


@app.route("/api/admin/delete-clip/<clip_id>", methods=["DELETE"])
def admin_delete_clip(clip_id):
    db = get_db()
    db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/admin/reset", methods=["POST"])
def admin_reset():
    db = get_db()
    db.execute("DELETE FROM leaderboard")
    db.execute("DELETE FROM games")
    db.commit()
    return jsonify({"status": "reset"})


@app.route("/api/admin/players")
@require_admin
def admin_players():
    db = get_db()
    rows = db.execute("""
        SELECT p.id, p.nickname, p.email, p.created_at,
               MAX(l.score) as best_score,
               MAX(l.points) as best_points,
               COUNT(DISTINCT g.id) as games_played
        FROM players p
        LEFT JOIN leaderboard l ON l.player_name = p.nickname
        LEFT JOIN games g ON g.player_name = p.nickname AND g.completed = 1
        GROUP BY p.id
        ORDER BY best_points DESC NULLS LAST
    """).fetchall()
    return jsonify({"players": [{
        "id": r["id"], "nickname": r["nickname"], "email": r["email"],
        "best_score": r["best_score"], "best_points": r["best_points"] or 0,
        "games_played": r["games_played"],
        "created_at": r["created_at"],
    } for r in rows]})


@app.route("/api/admin/players/<int:player_id>/games")
@require_admin
def admin_player_games(player_id):
    db = get_db()
    player = db.execute("SELECT nickname FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        return jsonify({"error": "unknown player"}), 404
    games = db.execute("""
        SELECT g.id, g.score, g.total, g.points, g.duration_sec, g.created_at
        FROM games g
        WHERE g.player_name = ? AND g.completed = 1
        ORDER BY g.points DESC, g.score DESC
    """, (player["nickname"],)).fetchall()
    return jsonify({"games": [{
        "id": g["id"], "score": g["score"], "total": g["total"],
        "points": g["points"] or 0,
        "duration": g["duration_sec"] or 0,
        "completed_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(g["created_at"])),
    } for g in games]})


@app.route("/api/admin/export")
def admin_export():
    auth = request.headers.get("Authorization", "")
    token_param = request.args.get("token", "")
    if not ((auth.startswith("Bearer ") and auth[7:] == ADMIN_TOKEN) or token_param == ADMIN_TOKEN):
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    rows = db.execute("""
        SELECT p.nickname, p.email, MAX(l.score) as best_score,
               MAX(l.points) as best_points,
               COUNT(DISTINCT g.id) as games_played
        FROM players p
        LEFT JOIN leaderboard l ON l.player_name = p.nickname
        LEFT JOIN games g ON g.player_name = p.nickname AND g.completed = 1
        GROUP BY p.id
        ORDER BY best_points DESC NULLS LAST
    """).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rank", "nickname", "email", "best_score", "best_points", "games_played"])
    for i, r in enumerate(rows):
        writer.writerow([i + 1, r["nickname"], r["email"], r["best_score"] or 0, r["best_points"] or 0, r["games_played"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=voice-id-results.csv"})


@app.route("/api/stats/most-mistaken-humans")
def most_mistaken_humans():
    db = get_db()
    rows = db.execute("""
        SELECT c.speaker,
               SUM(cs.times_guessed_ai) as mistaken_ai,
               SUM(cs.times_played) as total_guesses
        FROM clip_stats cs
        JOIN clips c ON c.id = cs.clip_id
        WHERE c.is_ai = 0 AND cs.times_played > 0
        GROUP BY c.speaker
        ORDER BY CAST(SUM(cs.times_guessed_ai) AS REAL) / SUM(cs.times_played) DESC
    """).fetchall()
    return jsonify([{
        "speaker": r["speaker"],
        "mistaken_for_ai": r["mistaken_ai"],
        "total_guesses": r["total_guesses"],
        "mistake_rate": round(r["mistaken_ai"] / r["total_guesses"], 3) if r["total_guesses"] else 0,
    } for r in rows])


@app.route("/api/stats/most-mistaken-clips")
def most_mistaken_clips():
    db = get_db()
    rows = db.execute("""
        SELECT c.id, c.filename, c.speaker, c.language, c.is_ai,
               cs.times_guessed_ai, cs.times_guessed_human, cs.times_played
        FROM clip_stats cs
        JOIN clips c ON c.id = cs.clip_id
        WHERE c.is_ai = 0 AND cs.times_played > 0
        ORDER BY CAST(cs.times_guessed_ai AS REAL) / cs.times_played DESC
    """).fetchall()
    return jsonify([{
        "clip_id": r["id"],
        "filename": r["filename"],
        "speaker": r["speaker"],
        "language": r["language"],
        "mistaken_for_ai": r["times_guessed_ai"],
        "correctly_guessed": r["times_guessed_human"],
        "total_guesses": r["times_played"],
        "mistake_rate": round(r["times_guessed_ai"] / r["times_played"], 3) if r["times_played"] else 0,
    } for r in rows])


@app.route("/api/admin/reset-scores", methods=["POST"])
@require_admin
def admin_reset_scores():
    db = get_db()
    db.execute("DELETE FROM leaderboard")
    db.execute("DELETE FROM games")
    db.commit()
    return jsonify({"message": "All scores and games reset. Players kept."})


# --- Speaker profiles (personal reveals) ---
#
# API (voice-id has no OpenAPI spec; this block is the reference):
#
#   GET  /api/speakers
#        -> 200 [{name, display_name, has_photo, photo_url, human_clips, ai_clips}]
#           One entry per distinct non-empty clips.speaker (lowercased) plus any row in
#           the speakers table. display_name falls back to name with first letter
#           capitalised; photo_url is "" when there is no photo.
#   GET  /api/speakers/<name>/photo
#        -> 200 image bytes (stored Content-Type, Cache-Control, ETag) | 304 | 404
#   PUT  /api/admin/speakers/<name>          [admin Bearer token]
#        JSON {"display_name": "Anton G."}   (empty/null clears it -> fallback)
#        -> 200 speaker object (same shape as GET /api/speakers items) | 400 | 403
#   POST /api/admin/speakers/<name>/photo    [admin Bearer token]
#        multipart "photo" (JPEG/PNG/WebP, max 5 MB; detected by magic bytes).
#        Downscaled to max 512 px long edge + EXIF stripped when Pillow is available.
#        -> 200 speaker object | 400 invalid/unsupported | 413 too large | 403
#   DELETE /api/admin/speakers/<name>/photo  [admin Bearer token]
#        -> 200 speaker object (has_photo=false) | 403
#
#   POST /api/game/<game_id>/guess additionally returns:
#        speaker_display (str), speaker_photo (str URL or ""),
#        speaker_is_real_person (bool: speaker has >= 1 human clip, i.e. an AI clip
#        of this speaker is a clone of a real person).
#
# Only upload photos of people who explicitly agreed to be shown on the booth screen.

MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_MAX_EDGE = 512
MAX_SPEAKER_NAME = 64
MAX_DISPLAY_NAME = 80


def _speaker_key(name):
    return (name or "").strip().lower()


def _valid_speaker_key(key):
    return bool(key) and len(key) <= MAX_SPEAKER_NAME and "/" not in key and "\\" not in key


def _speaker_photo_url(key):
    return f"/api/speakers/{quote(key, safe='')}/photo"


def _default_display(key):
    return key[:1].upper() + key[1:] if key else ""


def _speaker_reveal(db, speaker):
    key = _speaker_key(speaker)
    if not key:
        return {"speaker_display": "", "speaker_photo": "", "speaker_is_real_person": False}
    prof = db.execute(
        "SELECT display_name, photo IS NOT NULL AS has_photo FROM speakers WHERE name = ?", (key,)
    ).fetchone()
    is_real = db.execute(
        "SELECT 1 FROM clips WHERE is_ai = 0 AND LOWER(TRIM(speaker)) = ? LIMIT 1", (key,)
    ).fetchone() is not None
    display = (prof["display_name"] if prof else None) or _default_display((speaker or "").strip())
    return {
        "speaker_display": display,
        "speaker_photo": _speaker_photo_url(key) if prof and prof["has_photo"] else "",
        "speaker_is_real_person": is_real,
    }


def _speaker_list(db, only=None):
    counts = {}
    for r in db.execute(
        "SELECT LOWER(TRIM(speaker)) AS k, SUM(is_ai = 0) AS human, SUM(is_ai = 1) AS ai "
        "FROM clips WHERE TRIM(COALESCE(speaker, '')) != '' GROUP BY k"
    ).fetchall():
        counts[r["k"]] = (r["human"] or 0, r["ai"] or 0)
    profiles = {r["name"]: r for r in db.execute(
        "SELECT name, display_name, photo IS NOT NULL AS has_photo FROM speakers"
    ).fetchall()}
    keys = sorted(set(counts) | set(profiles))
    if only is not None:
        keys = [k for k in keys if k == only]
    out = []
    for k in keys:
        prof = profiles.get(k)
        has_photo = bool(prof and prof["has_photo"])
        human, ai = counts.get(k, (0, 0))
        out.append({
            "name": k,
            "display_name": (prof["display_name"] if prof else None) or _default_display(k),
            "has_photo": has_photo,
            "photo_url": _speaker_photo_url(k) if has_photo else "",
            "human_clips": human,
            "ai_clips": ai,
        })
    return out


def _speaker_obj(db, key):
    items = _speaker_list(db, only=key)
    return items[0] if items else {
        "name": key, "display_name": _default_display(key), "has_photo": False,
        "photo_url": "", "human_clips": 0, "ai_clips": 0,
    }


def _sniff_image_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _process_photo(data, mime):
    """Validate/downscale with Pillow if available. Returns (bytes, mime) or raises ValueError."""
    if not HAS_PIL:
        return data, mime
    fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[mime]
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # corrupt file, decompression bomb, unsupported codec
        raise ValueError(f"cannot decode image: {e}")
    img = ImageOps.exif_transpose(img)  # phone photos: honour orientation before EXIF is dropped
    img.thumbnail((PHOTO_MAX_EDGE, PHOTO_MAX_EDGE))
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    save_kwargs = {"quality": 85} if fmt in ("JPEG", "WEBP") else {"optimize": True}
    img.save(out, format=fmt, **save_kwargs)  # re-encode strips EXIF (GPS etc.)
    return out.getvalue(), mime


@app.route("/api/speakers")
def api_speakers():
    return jsonify(_speaker_list(get_db()))


@app.route("/api/speakers/<name>/photo")
def api_speaker_photo(name):
    key = _speaker_key(name)
    row = get_db().execute(
        "SELECT photo, photo_type, updated_at FROM speakers WHERE name = ? AND photo IS NOT NULL", (key,)
    ).fetchone()
    if not row:
        return jsonify({"error": "no photo"}), 404
    etag = f'"{key}-{int((row["updated_at"] or 0) * 1000)}"'
    headers = {"Cache-Control": "public, max-age=60", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    data = row["photo"]
    headers["Content-Length"] = str(len(data))
    return Response(data, mimetype=row["photo_type"] or "image/jpeg", headers=headers)


@app.route("/api/admin/speakers/<name>", methods=["PUT"])
@require_admin
def admin_update_speaker(name):
    key = _speaker_key(name)
    if not _valid_speaker_key(key):
        return jsonify({"error": "invalid speaker name"}), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "display_name" not in data:
        return jsonify({"error": "JSON body with display_name required"}), 400
    display = (data.get("display_name") or "").strip()
    if len(display) > MAX_DISPLAY_NAME:
        return jsonify({"error": f"display_name max {MAX_DISPLAY_NAME} chars"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO speakers (name, display_name, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at",
        (key, display or None, time.time()),
    )
    db.commit()
    logger.info("SPEAKER_UPDATE name=%s display=%s", key, display or "-")
    return jsonify(_speaker_obj(db, key))


@app.route("/api/admin/speakers/<name>/photo", methods=["POST"])
@require_admin
def admin_upload_speaker_photo(name):
    key = _speaker_key(name)
    if not _valid_speaker_key(key):
        return jsonify({"error": "invalid speaker name"}), 400
    if request.content_length and request.content_length > MAX_PHOTO_BYTES + 64 * 1024:
        return jsonify({"error": "photo too large (max 5 MB)"}), 413
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "photo file required"}), 400
    data = f.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        return jsonify({"error": "photo too large (max 5 MB)"}), 413
    mime = _sniff_image_type(data)
    if not mime:
        return jsonify({"error": "unsupported image type (use JPEG, PNG or WebP)"}), 400
    try:
        data, mime = _process_photo(data, mime)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db = get_db()
    db.execute(
        "INSERT INTO speakers (name, photo, photo_type, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET photo = excluded.photo, photo_type = excluded.photo_type, "
        "updated_at = excluded.updated_at",
        (key, data, mime, time.time()),
    )
    db.commit()
    logger.info("SPEAKER_PHOTO name=%s type=%s size=%d resized=%s", key, mime, len(data), HAS_PIL)
    return jsonify(_speaker_obj(db, key))


@app.route("/api/admin/speakers/<name>/photo", methods=["DELETE"])
@require_admin
def admin_delete_speaker_photo(name):
    key = _speaker_key(name)
    db = get_db()
    db.execute("UPDATE speakers SET photo = NULL, photo_type = NULL, updated_at = ? WHERE name = ?",
               (time.time(), key))
    db.commit()
    logger.info("SPEAKER_PHOTO_DELETE name=%s", key)
    return jsonify(_speaker_obj(db, key))


# --- Seed packs & backup ---
#
# Audio never goes to git (public repo). Teams distribute clips + speaker profiles as a
# private "seed pack" zip exported from a running instance. API:
#
#   GET  /api/admin/seedpack            [admin: Bearer header or ?token=]
#        -> 200 application/zip "voice-id-seedpack-<YYYYMMDD-HHMM>.seedpack.zip"
#           manifest.json {format: "voice-id-seedpack", version: 1, exported_at, app_version,
#             clips: [{filename, is_ai, language, source, speaker, content_type, sha256, bytes,
#                      file: "clips/<safe name>"}],
#             speakers: [{name, display_name, photo_file|null, photo_type|null}]}
#           + clips/* + speakers/*. NO players/games/leaderboard/clip_stats/emails.
#   POST /api/admin/seedpack            [admin: Bearer header]
#        multipart "seedpack" (zip, max 1 GB), optional form "mode" = skip (default) | replace
#        -> 200 {status: "imported", mode, clips: {added, skipped, conflicts, replaced, invalid},
#                speakers: {added, updated, skipped, invalid}, errors: [...]}
#        -> 400 same shape with status "rejected" (nothing written) when the pack is not a zip,
#           has the wrong format/version, exceeds limits, or ANY clip/speaker fails validation
#           (sha256 mismatch, non-audio content, bad metadata, missing entry). All-or-nothing:
#           the pack is validated completely before one write transaction.
#        Matching: same filename + same sha256 -> skipped; same filename, different content ->
#        conflict (skip mode, untouched) or replaced in place (replace mode, clip id kept).
#   GET  /api/admin/backup              [admin: Bearer header or ?token=]
#        -> 200 consistent SQLite snapshot (sqlite3 backup API, WAL-safe).
#           CONTAINS PERSONAL DATA (player nicknames/emails). Operator DR only - never share.

SEEDPACK_FORMAT = "voice-id-seedpack"
SEEDPACK_VERSION = 1
SEEDPACK_MAX_UPLOAD = 1024 ** 3            # 1 GB compressed upload
SEEDPACK_MAX_TOTAL = 2 * 1024 ** 3         # 2 GB uncompressed
SEEDPACK_MAX_ENTRY = 50 * 1024 * 1024      # 50 MB per clip
SEEDPACK_MAX_ENTRIES = 20000
SEEDPACK_MAX_MANIFEST = 20 * 1024 * 1024
APP_VERSION = os.environ.get("APP_VERSION", "") or None

_AUDIO_EXT = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a",
              "audio/ogg": "ogg", "audio/flac": "flac", "audio/webm": "webm"}
_IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _is_admin_request(allow_query_token=False):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == ADMIN_TOKEN:
        return True
    return allow_query_token and request.args.get("token", "") == ADMIN_TOKEN


def _sniff_audio_type(data):
    """Magic-byte check; returns normalised audio MIME type or None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    return None


def _safe_zip_name(s, maxlen=120):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").replace("/", "__")).strip("._") or "x"
    return s[-maxlen:]


def _send_temp_file(path, download_name, mimetype, extra_headers=None):
    """Stream a temp file and remove it from disk immediately (POSIX keeps the open inode)."""
    f = open(path, "rb")
    os.unlink(path)
    resp = send_file(f, mimetype=mimetype, as_attachment=True, download_name=download_name,
                     max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    for k, v in (extra_headers or {}).items():
        resp.headers[k] = v
    return resp


@app.route("/api/admin/seedpack", methods=["GET"])
def admin_seedpack_export():
    if not _is_admin_request(allow_query_token=True):
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    fd, tmp_path = tempfile.mkstemp(prefix="voiceid-seedpack-", suffix=".zip")
    os.close(fd)
    clips_meta, speakers_meta = [], []
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            ids = [r["id"] for r in db.execute("SELECT id FROM clips ORDER BY created_at, id").fetchall()]
            for i, cid in enumerate(ids):
                row = db.execute(
                    "SELECT filename, is_ai, language, source, speaker, content_type, audio_data "
                    "FROM clips WHERE id = ?", (cid,)).fetchone()
                if not row:
                    continue
                data = row["audio_data"]
                ctype = _sniff_audio_type(data) or row["content_type"] or "application/octet-stream"
                base = _safe_zip_name(row["filename"])
                ext = _AUDIO_EXT.get(ctype)
                if ext and not base.lower().endswith("." + ext):
                    base += "." + ext
                arc = f"clips/{i:05d}_{base}"
                zf.writestr(arc, data)
                clips_meta.append({
                    "filename": row["filename"], "is_ai": int(row["is_ai"]), "language": row["language"],
                    "source": row["source"] or "", "speaker": row["speaker"] or "",
                    "content_type": ctype, "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "file": arc,
                })
                del data, row
            for j, r in enumerate(db.execute(
                    "SELECT name, display_name, photo, photo_type FROM speakers ORDER BY name").fetchall()):
                photo_file = None
                if r["photo"] is not None:
                    ext = _IMAGE_EXT.get(r["photo_type"] or "", "bin")
                    photo_file = f"speakers/{j:04d}_{_safe_zip_name(r['name'], 60)}.{ext}"
                    zf.writestr(photo_file, r["photo"])
                speakers_meta.append({"name": r["name"], "display_name": r["display_name"],
                                      "photo_file": photo_file,
                                      "photo_type": r["photo_type"] if photo_file else None})
            zf.writestr("manifest.json", json.dumps({
                "format": SEEDPACK_FORMAT, "version": SEEDPACK_VERSION,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "app_version": APP_VERSION,
                "clips": clips_meta, "speakers": speakers_meta,
            }, indent=2, ensure_ascii=False))
    except Exception:
        os.unlink(tmp_path)
        raise
    name = f"voice-id-seedpack-{time.strftime('%Y%m%d-%H%M')}.seedpack.zip"
    logger.info("SEEDPACK_EXPORT clips=%d speakers=%d size=%d",
                len(clips_meta), len(speakers_meta), os.path.getsize(tmp_path))
    return _send_temp_file(tmp_path, name, "application/zip")


def _read_entry(zf, info, limit):
    """Read a zip entry with a hard cap (declared sizes can lie)."""
    with zf.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"entry exceeds {limit} bytes")
    return data


def _valid_meta_str(v, maxlen, allow_empty=True):
    return isinstance(v, str) and len(v) <= maxlen and "\x00" not in v and (allow_empty or v.strip())


def _validate_clip_meta(c):
    if not isinstance(c, dict):
        return "clip entry is not an object"
    fn = c.get("filename")
    if not _valid_meta_str(fn, 255, allow_empty=False):
        return "invalid filename"
    parts = fn.replace("\\", "/").split("/")
    if fn.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", fn):
        return f"unsafe filename {fn!r}"
    if c.get("is_ai") not in (0, 1, True, False):
        return f"{fn}: is_ai must be 0/1"
    if not (isinstance(c.get("language"), str) and re.fullmatch(r"[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})?", c["language"])):
        return f"{fn}: invalid language"
    for k in ("source", "speaker"):
        if not _valid_meta_str(c.get(k, ""), 200):
            return f"{fn}: invalid {k}"
    if not (isinstance(c.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", c["sha256"])):
        return f"{fn}: invalid sha256"
    if not isinstance(c.get("bytes"), int) or not 0 < c["bytes"] <= SEEDPACK_MAX_ENTRY:
        return f"{fn}: invalid bytes"
    if not _valid_meta_str(c.get("file"), 300, allow_empty=False):
        return f"{fn}: invalid file reference"
    return None


def _seedpack_summary(mode, status):
    return {"status": status, "mode": mode,
            "clips": {"added": 0, "skipped": 0, "conflicts": 0, "replaced": 0, "invalid": 0},
            "speakers": {"added": 0, "updated": 0, "skipped": 0, "invalid": 0},
            "errors": []}


@app.route("/api/admin/seedpack", methods=["POST"])
def admin_seedpack_import():
    if not _is_admin_request():
        return jsonify({"error": "Forbidden"}), 403
    request.max_content_length = SEEDPACK_MAX_UPLOAD  # per-route limit (Flask >= 3.1)
    mode = request.form.get("mode", "skip")
    if mode not in ("skip", "replace"):
        return jsonify({"error": "mode must be skip or replace"}), 400
    up = request.files.get("seedpack")
    if not up:
        return jsonify({"error": "seedpack file required"}), 400

    summary = _seedpack_summary(mode, "rejected")

    def reject(msg, code=400):
        summary["errors"].append(msg)
        logger.warning("SEEDPACK_IMPORT rejected: %s", msg)
        return jsonify(summary), code

    fd, tmp_path = tempfile.mkstemp(prefix="voiceid-import-", suffix=".zip")
    os.close(fd)
    try:
        up.save(tmp_path)
        if not zipfile.is_zipfile(tmp_path):
            return reject("not a zip file")
        with zipfile.ZipFile(tmp_path) as zf:
            infos = zf.infolist()
            if len(infos) > SEEDPACK_MAX_ENTRIES:
                return reject(f"too many entries ({len(infos)} > {SEEDPACK_MAX_ENTRIES})")
            if sum(i.file_size for i in infos) > SEEDPACK_MAX_TOTAL:
                return reject("pack exceeds 2 GB uncompressed")
            by_name = {}
            for i in infos:
                if i.is_dir():
                    continue
                if i.filename in by_name:
                    return reject(f"duplicate zip entry {i.filename!r}")
                by_name[i.filename] = i
            if "manifest.json" not in by_name:
                return reject("manifest.json missing - not a Voice ID seed pack")
            try:
                manifest = json.loads(_read_entry(zf, by_name["manifest.json"], SEEDPACK_MAX_MANIFEST))
            except Exception as e:
                return reject(f"manifest.json unreadable: {e}")
            if not isinstance(manifest, dict) or manifest.get("format") != SEEDPACK_FORMAT:
                return reject("wrong format (expected voice-id-seedpack)")
            if manifest.get("version") != SEEDPACK_VERSION:
                return reject(f"unsupported seed pack version {manifest.get('version')!r} (supported: 1)")
            clips = manifest.get("clips")
            speakers = manifest.get("speakers", [])
            if not isinstance(clips, list) or not isinstance(speakers, list):
                return reject("manifest clips/speakers must be lists")

            # ---- Phase 1: validate everything, write nothing ----
            total = 0
            for c in clips:
                err = _validate_clip_meta(c)
                info = by_name.get(c.get("file")) if not err else None
                if not err and info is None:
                    err = f"{c['filename']}: entry {c.get('file')!r} missing in zip"
                if not err:
                    try:
                        data = _read_entry(zf, info, SEEDPACK_MAX_ENTRY)
                    except Exception as e:
                        data, err = None, f"{c['filename']}: {e}"
                if not err:
                    total += len(data)
                    if total > SEEDPACK_MAX_TOTAL:
                        return reject("pack exceeds 2 GB uncompressed")
                    if len(data) != c["bytes"]:
                        err = f"{c['filename']}: size mismatch"
                    elif hashlib.sha256(data).hexdigest() != c["sha256"]:
                        err = f"{c['filename']}: sha256 mismatch"
                    elif not _sniff_audio_type(data):
                        err = f"{c['filename']}: content is not a supported audio format"
                if err:
                    summary["clips"]["invalid"] += 1
                    summary["errors"].append(err)
            spk_valid = []
            for sp in speakers:
                err = None
                key = _speaker_key(sp.get("name")) if isinstance(sp, dict) and isinstance(sp.get("name"), str) else ""
                if not _valid_speaker_key(key):
                    err = f"invalid speaker name {sp.get('name') if isinstance(sp, dict) else sp!r}"
                elif sp.get("display_name") is not None and not _valid_meta_str(sp["display_name"], MAX_DISPLAY_NAME):
                    err = f"speaker {key}: invalid display_name"
                photo, ptype = None, None
                if not err and sp.get("photo_file"):
                    info = by_name.get(sp["photo_file"]) if isinstance(sp["photo_file"], str) else None
                    if info is None:
                        err = f"speaker {key}: photo entry missing in zip"
                    else:
                        try:
                            photo = _read_entry(zf, info, MAX_PHOTO_BYTES)
                            ptype = _sniff_image_type(photo)
                            if not ptype:
                                err = f"speaker {key}: photo is not JPEG/PNG/WebP"
                        except Exception as e:
                            err = f"speaker {key}: {e}"
                if err:
                    summary["speakers"]["invalid"] += 1
                    summary["errors"].append(err)
                else:
                    spk_valid.append((key, (sp.get("display_name") or "").strip() or None, photo, ptype))
            if summary["clips"]["invalid"] or summary["speakers"]["invalid"]:
                logger.warning("SEEDPACK_IMPORT rejected: %d invalid clips, %d invalid speakers",
                               summary["clips"]["invalid"], summary["speakers"]["invalid"])
                return jsonify(summary), 400

            # ---- Phase 2: one transaction for the whole pack ----
            db = get_db()
            existing = {}
            for r in db.execute("SELECT id, filename FROM clips").fetchall():
                existing.setdefault(r["filename"], []).append(r["id"])
            sha_cache = {}

            def clip_sha(cid):
                if cid not in sha_cache:
                    row = db.execute("SELECT audio_data FROM clips WHERE id = ?", (cid,)).fetchone()
                    sha_cache[cid] = hashlib.sha256(row["audio_data"]).hexdigest()
                return sha_cache[cid]

            try:
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                for c in clips:
                    fn = c["filename"]
                    ids = existing.get(fn, [])
                    if any(clip_sha(cid) == c["sha256"] for cid in ids):
                        summary["clips"]["skipped"] += 1
                        continue
                    if ids and mode == "skip":
                        summary["clips"]["conflicts"] += 1
                        summary["errors"].append(f"conflict (kept existing): {fn}")
                        continue
                    data = _read_entry(zf, by_name[c["file"]], SEEDPACK_MAX_ENTRY)
                    if hashlib.sha256(data).hexdigest() != c["sha256"]:
                        raise ValueError(f"{fn}: sha256 changed between validation and import")
                    ctype = _sniff_audio_type(data)
                    vals = (int(bool(c["is_ai"])), c["language"], c.get("source", ""), c.get("speaker", ""), data, ctype)
                    if ids:
                        db.execute("UPDATE clips SET is_ai = ?, language = ?, source = ?, speaker = ?, "
                                   "audio_data = ?, content_type = ? WHERE id = ?", vals + (ids[0],))
                        sha_cache[ids[0]] = c["sha256"]
                        summary["clips"]["replaced"] += 1
                    else:
                        cid = str(uuid.uuid4())[:8]
                        while db.execute("SELECT 1 FROM clips WHERE id = ?", (cid,)).fetchone():
                            cid = str(uuid.uuid4())[:8]
                        db.execute("INSERT INTO clips (id, filename, is_ai, language, source, speaker, audio_data, "
                                   "content_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (cid, fn) + vals + (now,))
                        existing.setdefault(fn, []).append(cid)
                        sha_cache[cid] = c["sha256"]
                        summary["clips"]["added"] += 1
                for key, display, photo, ptype in spk_valid:
                    cur = db.execute("SELECT display_name, photo, photo IS NOT NULL AS has_photo FROM speakers WHERE name = ?",
                                     (key,)).fetchone()
                    if not cur:
                        db.execute("INSERT INTO speakers (name, display_name, photo, photo_type, updated_at) "
                                   "VALUES (?, ?, ?, ?, ?)", (key, display, photo, ptype, now))
                        summary["speakers"]["added"] += 1
                        continue
                    set_display = (display is not None and display != cur["display_name"]
                                   and (mode == "replace" or not cur["display_name"]))
                    set_photo = (photo is not None and photo != cur["photo"]
                                 and (mode == "replace" or not cur["has_photo"]))
                    if set_display:
                        db.execute("UPDATE speakers SET display_name = ?, updated_at = ? WHERE name = ?",
                                   (display, now, key))
                    if set_photo:
                        db.execute("UPDATE speakers SET photo = ?, photo_type = ?, updated_at = ? WHERE name = ?",
                                   (photo, ptype, now, key))
                    summary["speakers"]["updated" if (set_display or set_photo) else "skipped"] += 1
                db.commit()
            except Exception as e:
                db.rollback()
                logger.exception("SEEDPACK_IMPORT failed, rolled back")
                summary["errors"].append(f"import failed, nothing written (rolled back): {e}")
                summary["clips"].update(added=0, replaced=0)
                summary["speakers"].update(added=0, updated=0)
                return jsonify(summary), 500
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    summary["status"] = "imported"
    logger.info("SEEDPACK_IMPORT mode=%s clips=%s speakers=%s", mode, summary["clips"], summary["speakers"])
    return jsonify(summary)


@app.route("/api/admin/backup")
def admin_backup():
    if not _is_admin_request(allow_query_token=True):
        return jsonify({"error": "Forbidden"}), 403
    fd, tmp_path = tempfile.mkstemp(prefix="voiceid-backup-", suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(str(DATA_DIR / "voiceid.db"))
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)  # consistent snapshot, safe while the app writes (WAL)
        finally:
            dst.close()
            src.close()
    except Exception:
        os.unlink(tmp_path)
        raise
    name = f"voice-id-FULL-BACKUP-CONTAINS-PERSONAL-DATA-{time.strftime('%Y%m%d-%H%M')}.db"
    logger.info("BACKUP_DOWNLOAD size=%d", os.path.getsize(tmp_path))
    return _send_temp_file(tmp_path, name, "application/vnd.sqlite3",
                           {"X-Contains-Personal-Data": "true"})


# --- AI Judge ---

def convert_to_wav_pcm(audio_bytes):
    # Write to temp file — m4a/mp4 containers need seeking, which pipe:0 can't do
    with tempfile.NamedTemporaryFile(suffix='.audio') as tmp_in:
        tmp_in.write(audio_bytes)
        tmp_in.flush()
        proc = subprocess.run(
            [FFMPEG, "-i", tmp_in.name, "-ac", "1", "-ar", "22050",
             "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            capture_output=True,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[-200:]}")
    return proc.stdout


def generate_spectrogram(wav_bytes):
    buf = io.BytesIO(wav_bytes)
    sample_rate, audio = scipy_wavfile.read(buf)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)
    if len(audio) == 0:
        raise ValueError("Empty audio array after conversion")
    max_val = np.max(np.abs(audio))
    if max_val > 0:
        audio = audio / max_val

    fig, ax = plt.subplots(1, 1, figsize=(10, 4))
    ax.specgram(audio, Fs=sample_rate, NFFT=1024, noverlap=768, cmap='magma',
                scale='dB')
    ax.set_ylim(0, 8000)
    ax.set_ylabel('Frequency (Hz)', color='#8ab0c0')
    ax.set_xlabel('Time (s)', color='#8ab0c0')
    ax.tick_params(colors='#8ab0c0')
    fig.patch.set_facecolor('#0f2a38')
    ax.set_facecolor('#0f2a38')

    out = io.BytesIO()
    fig.savefig(out, format='png', dpi=100, bbox_inches='tight', facecolor='#0f2a38')
    plt.close(fig)
    out.seek(0)
    return out.read()


def _parse_judge_response(content):
    try:
        text = content.strip()
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
            text = text.strip()
        if text.startswith('{'):
            return json.loads(text)
        brace_start = text.find('{')
        brace_end = text.rfind('}')
        if brace_start >= 0 and brace_end > brace_start:
            return json.loads(text[brace_start:brace_end + 1])
        return json.loads(text)
    except (json.JSONDecodeError, IndexError, ValueError):
        content_lower = content.lower()
        guess = "human" if "human" in content_lower else "ai"
        return {"guess": guess, "reasoning": content[:200]}


def ai_judge_analyze(spectrogram_png):
    img_b64 = base64.b64encode(spectrogram_png).decode()

    payload = {
        "model": AI_JUDGE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are an audio forensics expert examining a spectrogram "
                            "(frequency vs time) of a voice recording.\n\n"
                            "TASK: Is this AI-generated (text-to-speech) or natural human speech?\n\n"
                            "AI-GENERATED (TTS) markers:\n"
                            "- Harmonics appear as clean, sharp, evenly-spaced horizontal bands\n"
                            "- Very little noise or haze between harmonic lines\n"
                            "- Energy is unnaturally uniform — same intensity across the whole clip\n"
                            "- Formant bands are smooth, mechanical, almost ruler-straight\n"
                            "- Background is unusually clean (minimal broadband noise)\n"
                            "- Periodic patterns repeat with machine-like regularity\n\n"
                            "NATURAL HUMAN markers:\n"
                            "- Harmonics waver and vary in width and intensity\n"
                            "- Noise/haze between bands (breath, room acoustics)\n"
                            "- Visible gaps or energy changes (breathing pauses, emphasis)\n"
                            "- Formant bands shift dynamically and unpredictably\n"
                            "- Micro pitch variations visible as undulating harmonic lines\n"
                            "- More broadband noise as light haze across frequencies\n\n"
                            "Look carefully at the spectrogram. Identify which set of markers is more prominent.\n\n"
                            "Respond with ONLY this JSON (no markdown, no extra text):\n"
                            '{"guess": "ai", "reasoning": "one sentence about key visual evidence"}\n'
                            "or\n"
                            '{"guess": "human", "reasoning": "one sentence about key visual evidence"}'
                        )
                    }
                ]
            }
        ],
        "max_tokens": 200,
        "temperature": 0.1,
    }

    resp = http_requests.post(
        f"{AI_JUDGE_URL}/chat/completions",
        json=payload,
        timeout=60,
        verify=False,
    )
    resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_judge_response(content)


@app.route("/api/admin/spectrogram/<clip_id>")
def admin_spectrogram(clip_id):
    if not HAS_SPECTROGRAM:
        return jsonify({"error": "spectrogram libs not available"}), 503
    db = get_db()
    clip = db.execute("SELECT audio_data, filename FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not clip:
        return jsonify({"error": "clip not found"}), 404
    try:
        wav_bytes = convert_to_wav_pcm(clip["audio_data"])
        png = generate_spectrogram(wav_bytes)
        return Response(png, mimetype="image/png",
                        headers={"Content-Disposition": f'inline; filename="spectrogram-{clip_id}.png"'})
    except Exception:
        logger.exception("Spectrogram failed for clip %s", clip_id)
        return jsonify({"error": "generation failed"}), 500


def ai_judge2_analyze(wav_bytes):
    audio_b64 = base64.b64encode(wav_bytes).decode()

    payload = {
        "model": AI_JUDGE2_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": f"data:audio/wav;base64,{audio_b64}"}
                    },
                    {
                        "type": "text",
                        "text": (
                            "Listen to this voice recording carefully.\n\n"
                            "TASK: Is this AI-generated speech (text-to-speech synthesis) "
                            "or natural human speech?\n\n"
                            "AI-GENERATED speech clues:\n"
                            "- Unnaturally smooth intonation with no hesitation\n"
                            "- Perfect pronunciation with no stumbles or corrections\n"
                            "- Mechanical rhythm — words spaced too evenly\n"
                            "- Missing natural breathing sounds between phrases\n"
                            "- Robotic or metallic undertone in the voice\n\n"
                            "NATURAL HUMAN speech clues:\n"
                            "- Natural pitch variation and emphasis\n"
                            "- Audible breathing between phrases\n"
                            "- Slight imperfections (hesitations, lip smacks)\n"
                            "- Dynamic speaking pace — faster and slower sections\n"
                            "- Natural room acoustics and background noise\n\n"
                            "Respond with ONLY this JSON (no markdown, no extra text):\n"
                            '{"guess": "ai", "reasoning": "one sentence about key audio evidence"}\n'
                            "or\n"
                            '{"guess": "human", "reasoning": "one sentence about key audio evidence"}'
                        )
                    }
                ]
            }
        ],
        "max_tokens": 200,
        "temperature": 0.1,
    }

    resp = http_requests.post(
        f"{AI_JUDGE2_URL}/chat/completions",
        json=payload,
        timeout=120,
        verify=False,
    )
    resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_judge_response(content)


@app.route("/api/ai-judge/status")
def ai_judge_status():
    judge1 = bool(AI_JUDGE_URL) and HAS_SPECTROGRAM and HAS_REQUESTS
    judge2 = bool(AI_JUDGE2_URL) and HAS_REQUESTS
    detector = bool(DETECTOR_URL) and HAS_REQUESTS
    detector_base = bool(DETECTOR_BASE_URL) and HAS_REQUESTS
    return jsonify({
        "configured": judge1,
        "model": AI_JUDGE_MODEL if judge1 else None,
        "judge2_configured": judge2,
        "judge2_model": AI_JUDGE2_MODEL if judge2 else None,
        "detector_configured": detector,
        "detector_base_configured": detector_base,
    })


@app.route("/api/battle/opponents")
def battle_opponents():
    opponents = []
    if bool(AI_JUDGE_URL) and HAS_SPECTROGRAM and HAS_REQUESTS:
        opponents.append({
            "id": "gemma",
            "analyzeEndpoint": "ai-analyze",
            "available": True,
        })
    if bool(AI_JUDGE2_URL) and HAS_REQUESTS:
        opponents.append({
            "id": "qwen",
            "analyzeEndpoint": "ai-analyze2",
            "available": True,
        })
    if bool(DETECTOR_BASE_URL) and HAS_REQUESTS:
        opponents.append({
            "id": "gary-base",
            "analyzeEndpoint": "ai-analyze-detector-base",
            "available": True,
        })
    if bool(DETECTOR_URL) and HAS_REQUESTS:
        opponents.append({
            "id": "gary-finetuned",
            "analyzeEndpoint": "ai-analyze-detector",
            "available": True,
        })
    return jsonify(opponents)


@app.route("/api/game/<game_id>/ai-analyze", methods=["POST"])
def ai_analyze_clip(game_id):
    if not AI_JUDGE_URL or not HAS_SPECTROGRAM or not HAS_REQUESTS:
        return jsonify({"error": "AI judge not configured"}), 503

    data = request.get_json(force=True)
    clip_id = data.get("clip_id")
    if not clip_id:
        return jsonify({"error": "clip_id required"}), 400

    db = get_db()

    game = db.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "unknown game_id"}), 404

    clip = db.execute("SELECT audio_data, content_type FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not clip:
        return jsonify({"error": "unknown clip_id"}), 404

    try:
        wav_bytes = convert_to_wav_pcm(clip["audio_data"])
        spectrogram_png = generate_spectrogram(wav_bytes)
        spectrogram_b64 = base64.b64encode(spectrogram_png).decode()

        result = ai_judge_analyze(spectrogram_png)

        logger.info("AI_JUDGE clip=%s guess=%s reasoning=%s", clip_id,
                     result.get("guess"), result.get("reasoning", "")[:80])

        return jsonify({
            "guess": result.get("guess", "ai"),
            "reasoning": result.get("reasoning", ""),
            "spectrogram": f"data:image/png;base64,{spectrogram_b64}",
        })
    except Exception:
        logger.exception("AI analysis failed for clip %s", clip_id)
        return jsonify({"error": "Analysis failed"}), 500


@app.route("/api/game/<game_id>/ai-analyze2", methods=["POST"])
def ai_analyze2_clip(game_id):
    if not AI_JUDGE2_URL or not HAS_REQUESTS:
        return jsonify({"error": "AI judge 2 not configured"}), 503

    data = request.get_json(force=True)
    clip_id = data.get("clip_id")
    if not clip_id:
        return jsonify({"error": "clip_id required"}), 400

    db = get_db()

    game = db.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "unknown game_id"}), 404

    clip = db.execute("SELECT audio_data, content_type FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not clip:
        return jsonify({"error": "unknown clip_id"}), 404

    try:
        wav_bytes = convert_to_wav_pcm(clip["audio_data"])
        result = ai_judge2_analyze(wav_bytes)

        logger.info("AI_JUDGE2 clip=%s guess=%s reasoning=%s", clip_id,
                     result.get("guess"), result.get("reasoning", "")[:80])

        return jsonify({
            "guess": result.get("guess", "ai"),
            "reasoning": result.get("reasoning", ""),
        })
    except Exception:
        logger.exception("AI judge2 analysis failed for clip %s", clip_id)
        return jsonify({"error": "Analysis failed"}), 500


def _analyze_with_detector(game_id, clip_id, detector_url):
    db = get_db()
    game = db.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "unknown game_id"}), 404

    clip = db.execute("SELECT audio_data, content_type FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not clip:
        return jsonify({"error": "unknown clip_id"}), 404

    try:
        wav_bytes = convert_to_wav_pcm(clip["audio_data"])

        resp = http_requests.post(
            f"{detector_url}/predict",
            files={"audio": ("clip.wav", wav_bytes, "audio/wav")},
            timeout=60,
            verify=False,
        )
        resp.raise_for_status()
        result = resp.json()

        guess = "ai" if result["verdict"] == "SYNTHETIC" else "human"
        reasoning = f'{result["verdict"]} (confidence: {result["confidence"]:.1%}, p_fake: {result["prob_fake"]:.4f})'

        logger.info("DETECTOR clip=%s verdict=%s confidence=%.3f p_fake=%.4f",
                     clip_id, result["verdict"], result["confidence"], result["prob_fake"])

        return jsonify({
            "guess": guess,
            "reasoning": reasoning,
            "verdict": result["verdict"],
            "confidence": result["confidence"],
            "prob_fake": result["prob_fake"],
        })
    except Exception:
        logger.exception("Detector analysis failed for clip %s", clip_id)
        return jsonify({"error": "Analysis failed"}), 500


@app.route("/api/game/<game_id>/ai-analyze-detector", methods=["POST"])
def ai_analyze_detector(game_id):
    if not DETECTOR_URL or not HAS_REQUESTS:
        return jsonify({"error": "Detector not configured"}), 503
    data = request.get_json(force=True)
    clip_id = data.get("clip_id")
    if not clip_id:
        return jsonify({"error": "clip_id required"}), 400
    return _analyze_with_detector(game_id, clip_id, DETECTOR_URL)


@app.route("/api/game/<game_id>/ai-analyze-detector-base", methods=["POST"])
def ai_analyze_detector_base(game_id):
    if not DETECTOR_BASE_URL or not HAS_REQUESTS:
        return jsonify({"error": "Detector base not configured"}), 503
    data = request.get_json(force=True)
    clip_id = data.get("clip_id")
    if not clip_id:
        return jsonify({"error": "clip_id required"}), 400
    return _analyze_with_detector(game_id, clip_id, DETECTOR_BASE_URL)


with app.app_context():
    init_db()
