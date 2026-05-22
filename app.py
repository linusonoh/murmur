from pathlib import Path
import sqlite3
import sys


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "murmur.db"
MAX_CONTENT_LENGTH = 500
VENDOR_PATH = BASE_DIR / "vendor"

if VENDOR_PATH.exists():
    sys.path.insert(0, str(VENDOR_PATH))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (parent_id) REFERENCES entries(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_parent_created
            ON entries(parent_id, created_at)
            """
        )


def row_to_entry(row):
    return {
        "id": row["id"],
        "content": row["content"],
        "parent_id": row["parent_id"],
        "created_at": row["created_at"],
        "author": "Anonymous",
    }


def read_content():
    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()

    if not content:
        return None, ("Content cannot be blank.", 400)
    if len(content) > MAX_CONTENT_LENGTH:
        return None, (f"Content must be {MAX_CONTENT_LENGTH} characters or fewer.", 400)

    return content, None


def post_exists(post_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE id = ? AND parent_id IS NULL",
            (post_id,),
        ).fetchone()
    return row is not None


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found."}), 404


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.post("/api/posts")
def create_post():
    content, error = read_content()
    if error:
        message, status = error
        return jsonify({"error": message}), status

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO entries (content, parent_id) VALUES (?, NULL)",
            (content,),
        )
        post = conn.execute(
            "SELECT * FROM entries WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

    return jsonify(row_to_entry(post)), 201


@app.get("/api/posts")
def list_posts():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM entries
            WHERE parent_id IS NULL
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()

    return jsonify([row_to_entry(row) for row in rows])


@app.post("/api/posts/<int:post_id>/comments")
def create_comment(post_id):
    if not post_exists(post_id):
        return jsonify({"error": "Post not found."}), 404

    content, error = read_content()
    if error:
        message, status = error
        return jsonify({"error": message}), status

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO entries (content, parent_id) VALUES (?, ?)",
            (content, post_id),
        )
        comment = conn.execute(
            "SELECT * FROM entries WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

    return jsonify(row_to_entry(comment)), 201


@app.get("/api/posts/<int:post_id>/comments")
def list_comments(post_id):
    if not post_exists(post_id):
        return jsonify({"error": "Post not found."}), 404

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM entries
            WHERE parent_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (post_id,),
        ).fetchall()

    return jsonify([row_to_entry(row) for row in rows])


init_db()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
