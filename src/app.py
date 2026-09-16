import json
import os
import random
import sqlite3
import time
import uuid
from io import BytesIO
from pathlib import Path

import logging

from flask import Flask, Response, g, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
GAME_SIZE = int(os.environ.get("GAME_SIZE", "5"))
PRIZE_THRESHOLD = int(os.environ.get("PRIZE_THRESHOLD", "4"))
CONFERENCE_NAME = os.environ.get("CONFERENCE_NAME", "Red Hat Summit")
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "en")


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

        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            clip_ids TEXT NOT NULL,
            guesses TEXT DEFAULT '{}',
            score INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leaderboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name TEXT NOT NULL,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            duration_sec REAL,
            created_at REAL NOT NULL
        );
    """)
    conn.close()


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


# --- Audio serving ---

@app.route("/api/clip/<clip_id>/audio")
def serve_clip_audio(clip_id):
    db = get_db()
    row = db.execute("SELECT audio_data, content_type FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row:
        return jsonify({"error": "clip not found"}), 404
    return Response(row["audio_data"], mimetype=row["content_type"],
                    headers={"Content-Length": str(len(row["audio_data"]))})


# --- Game API ---

@app.route("/api/game/start", methods=["POST"])
def start_game():
    data = request.get_json(force=True)
    player_name = data.get("player_name", "").strip()
    if not player_name:
        return jsonify({"error": "player_name required"}), 400

    db = get_db()
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
    db.commit()

    return jsonify({
        "correct": correct,
        "actual": "ai" if actual_is_ai else "human",
        "source": clip["source"] or "",
        "speaker": clip["speaker"] or "",
        "language": clip["language"],
    })


@app.route("/api/game/<game_id>/complete", methods=["POST"])
def complete_game(game_id):
    db = get_db()
    row = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row:
        return jsonify({"error": "unknown game_id"}), 404

    guesses = json.loads(row["guesses"])
    score = sum(1 for g in guesses.values() if g["correct"])
    total = len(guesses)
    duration = time.time() - row["created_at"]

    db.execute(
        "UPDATE games SET score = ?, total = ?, completed = 1 WHERE id = ?",
        (score, total, game_id),
    )
    db.execute(
        "INSERT INTO leaderboard (player_name, score, total, duration_sec, created_at) VALUES (?, ?, ?, ?, ?)",
        (row["player_name"], score, total, duration, time.time()),
    )
    db.commit()

    rank_row = db.execute(
        "SELECT COUNT(*) as rank FROM leaderboard WHERE score > ? OR (score = ? AND duration_sec < ?)",
        (score, score, duration),
    ).fetchone()
    rank = rank_row["rank"] + 1

    return jsonify({
        "score": score,
        "total": total,
        "duration_sec": round(duration, 1),
        "rank": rank,
        "prize": score >= PRIZE_THRESHOLD,
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    db = get_db()
    rows = db.execute(
        "SELECT player_name, score, total, duration_sec, created_at "
        "FROM leaderboard ORDER BY score DESC, duration_sec ASC LIMIT 20"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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

    content_type = audio_file.content_type or "audio/wav"
    audio_data = audio_file.read()

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


with app.app_context():
    init_db()
