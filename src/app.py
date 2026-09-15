import json
import os
import random
import sqlite3
import time
import uuid
from io import BytesIO
from pathlib import Path

from flask import Flask, Response, g, jsonify, render_template, request

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
GAME_SIZE = int(os.environ.get("GAME_SIZE", "5"))
PRIZE_THRESHOLD = int(os.environ.get("PRIZE_THRESHOLD", "4"))
CONFERENCE_NAME = os.environ.get("CONFERENCE_NAME", "Red Hat Summit")


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
                           prize_threshold=PRIZE_THRESHOLD)


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
    return Response(row["audio_data"], mimetype=row["content_type"])


# --- Game API ---

@app.route("/api/game/start", methods=["POST"])
def start_game():
    data = request.get_json(force=True)
    player_name = data.get("player_name", "").strip()
    if not player_name:
        return jsonify({"error": "player_name required"}), 400

    db = get_db()
    clips = db.execute("SELECT id, language FROM clips").fetchall()

    if len(clips) < GAME_SIZE:
        return jsonify({"error": f"Not enough clips ({len(clips)}/{GAME_SIZE})"}), 500

    selected = random.sample([dict(c) for c in clips], GAME_SIZE)
    clip_ids = [c["id"] for c in selected]

    game_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO games (id, player_name, clip_ids, created_at) VALUES (?, ?, ?, ?)",
        (game_id, player_name, json.dumps(clip_ids), time.time()),
    )
    db.commit()

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


@app.route("/api/admin/reset", methods=["POST"])
def admin_reset():
    db = get_db()
    db.execute("DELETE FROM leaderboard")
    db.execute("DELETE FROM games")
    db.commit()
    return jsonify({"status": "reset"})


with app.app_context():
    init_db()
