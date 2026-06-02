from datetime import datetime, timedelta
import os
from pathlib import Path
import json
import secrets
import sqlite3
import sys
from threading import Lock
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parent
VENDOR_PATH = BASE_DIR / "vendor"
DATABASE_FILE = BASE_DIR / "murmur.db"
REPORTS_FILE = BASE_DIR / "reports.json"
MAX_CONTENT_LENGTH = 500
DEFAULT_ALIAS = "Anonymous"
DEFAULT_AVATAR_URL = "https://api.dicebear.com/9.x/bottts/svg?seed=Anonymous"
MAX_ALIAS_LENGTH = 32
MAX_SESSION_ID_LENGTH = 128
IDENTITY_TOKEN_MAX_AGE = 60 * 60 * 24 * 365
IDENTITY_TOKEN_SALT = "murmur-identity"

if VENDOR_PATH.exists():
    sys.path.insert(0, str(VENDOR_PATH))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("MURMUR_SECRET_KEY") or secrets.token_urlsafe(32)
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(
    app,
    resources={
        r"/*": {
            "origins": [
                "http://127.0.0.1:5000",
                "http://localhost:5000",
                "http://127.0.0.1:3000",
                "http://localhost:3000",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ],
            "methods": ["GET", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization", "Accept"],
        }
    },
)

storage_lock = Lock()
report_lock = Lock()
presence_lock = Lock()
connected_users = 0
typing_users = set()


def current_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def as_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def identity_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"])


def issue_identity_token(identity_id=None):
    identity_id = identity_id or uuid4().hex
    token = identity_serializer().dumps(
        {"identity_id": identity_id},
        salt=IDENTITY_TOKEN_SALT,
    )
    return token, identity_id


def verify_identity_token(token):
    try:
        data = identity_serializer().loads(
            token,
            salt=IDENTITY_TOKEN_SALT,
            max_age=IDENTITY_TOKEN_MAX_AGE,
        )
    except (BadSignature, SignatureExpired, TypeError):
        return None

    identity_id = str(data.get("identity_id", "")).strip() if isinstance(data, dict) else ""
    if not identity_id or len(identity_id) > MAX_SESSION_ID_LENGTH:
        return None
    return identity_id


def identity_from_payload(payload):
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    token = str(payload.get("identity_token", "")).strip()
    if not token:
        return None, "Identity token is required."

    identity_id = verify_identity_token(token)
    if not identity_id:
        return None, "Identity token is invalid or expired."

    return identity_id, None


def get_db():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def column_exists(connection, table_name, column_name):
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def ratings_has_cascade_fk(connection):
    if not connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ratings'"
    ).fetchone():
        return True

    rows = connection.execute("PRAGMA foreign_key_list(ratings)").fetchall()
    return any(
        row["table"] == "entries"
        and row["from"] == "post_id"
        and row["on_delete"].upper() == "CASCADE"
        for row in rows
    )


def ensure_ratings_table(connection):
    if not ratings_has_cascade_fk(connection):
        connection.execute("ALTER TABLE ratings RENAME TO ratings_legacy")
        connection.execute(
            """
            CREATE TABLE ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
                session_id TEXT,
                stars INTEGER,
                UNIQUE(post_id, session_id)
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO ratings (post_id, session_id, stars)
            SELECT ratings_legacy.post_id, ratings_legacy.session_id, ratings_legacy.stars
            FROM ratings_legacy
            JOIN entries ON entries.id = ratings_legacy.post_id
            """
        )
        connection.execute("DROP TABLE ratings_legacy")
    else:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
                session_id TEXT,
                stars INTEGER,
                UNIQUE(post_id, session_id)
            )
            """
        )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ratings_post_session
        ON ratings(post_id, session_id)
        """
    )


def seed_database_if_empty(connection):
    row = connection.execute("SELECT COUNT(*) AS entry_count FROM entries").fetchone()
    if row["entry_count"]:
        return

    now = datetime.now()
    seeds = [
        {
            "content": "I rebuilt this little corner of the internet so strangers can leave a thought and watch the stream answer back in real time.",
            "alias": "Night Signal",
            "avatar_url": "https://api.dicebear.com/9.x/bottts/svg?seed=NightSignal",
            "session_id": "seed-night-signal",
            "created_at": now - timedelta(minutes=28),
            "replies": [
                {
                    "content": "The live replies make it feel less like a wall and more like a room.",
                    "alias": "Echo Node",
                    "avatar_url": "https://api.dicebear.com/9.x/bottts/svg?seed=EchoNode",
                    "session_id": "seed-echo-node",
                    "created_at": now - timedelta(minutes=25),
                }
            ],
        },
        {
            "content": "Today I learned that shipping a tiny thing beats endlessly polishing the perfect version no one can use.",
            "alias": "Soft Launch",
            "avatar_url": "https://api.dicebear.com/9.x/bottts/svg?seed=SoftLaunch",
            "session_id": "seed-soft-launch",
            "created_at": now - timedelta(minutes=18),
            "replies": [
                {
                    "content": "The best portfolio projects feel alive. This one does.",
                    "alias": "Green Light",
                    "avatar_url": "https://api.dicebear.com/9.x/bottts/svg?seed=GreenLight",
                    "session_id": "seed-green-light",
                    "created_at": now - timedelta(minutes=14),
                }
            ],
        },
        {
            "content": "Some apps explain themselves with a paragraph. The better ones let you click once and understand the whole idea.",
            "alias": "Quiet Compiler",
            "avatar_url": "https://api.dicebear.com/9.x/bottts/svg?seed=QuietCompiler",
            "session_id": "seed-quiet-compiler",
            "created_at": now - timedelta(minutes=9),
            "replies": [],
        },
    ]

    for seed in seeds:
        cursor = connection.execute(
            """
            INSERT INTO entries (content, parent_id, created_at, session_id, alias, avatar_url)
            VALUES (?, NULL, ?, ?, ?, ?)
            """,
            (
                seed["content"],
                seed["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
                seed["session_id"],
                seed["alias"],
                seed["avatar_url"],
            ),
        )
        parent_id = cursor.lastrowid

        for reply in seed["replies"]:
            connection.execute(
                """
                INSERT INTO entries (content, parent_id, created_at, session_id, alias, avatar_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    reply["content"],
                    parent_id,
                    reply["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
                    reply["session_id"],
                    reply["alias"],
                    reply["avatar_url"],
                ),
            )


def initialize_database():
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                session_id TEXT,
                alias TEXT DEFAULT 'Anonymous',
                avatar_url TEXT DEFAULT '',
                total_rating_stars INTEGER DEFAULT 0,
                rating_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_id) REFERENCES entries(id) ON DELETE CASCADE
            )
            """
        )

        migrations = {
            "session_id": "ALTER TABLE entries ADD COLUMN session_id TEXT",
            "alias": "ALTER TABLE entries ADD COLUMN alias TEXT DEFAULT 'Anonymous'",
            "avatar_url": "ALTER TABLE entries ADD COLUMN avatar_url TEXT DEFAULT ''",
            "total_rating_stars": "ALTER TABLE entries ADD COLUMN total_rating_stars INTEGER DEFAULT 0",
            "rating_count": "ALTER TABLE entries ADD COLUMN rating_count INTEGER DEFAULT 0",
        }
        for column_name, statement in migrations.items():
            if not column_exists(connection, "entries", column_name):
                connection.execute(statement)

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_parent_created
            ON entries(parent_id, created_at)
            """
        )
        ensure_ratings_table(connection)
        seed_database_if_empty(connection)
        connection.commit()


def row_to_entry(row):
    total_rating_stars = as_int(row["total_rating_stars"], 0)
    rating_count = as_int(row["rating_count"], 0)
    average_rating = total_rating_stars / rating_count if rating_count else 0

    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "alias": row["alias"] or DEFAULT_ALIAS,
        "avatar_url": row["avatar_url"] or DEFAULT_AVATAR_URL,
        "content": row["content"],
        "created_at": str(row["created_at"]),
        "session_id": row["session_id"] or "",
        "total_rating_stars": total_rating_stars,
        "rating_count": rating_count,
        "average_rating": round(average_rating, 2),
    }


def load_entries():
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                content,
                parent_id,
                created_at,
                session_id,
                alias,
                avatar_url,
                total_rating_stars,
                rating_count
            FROM entries
            ORDER BY datetime(created_at) ASC, id ASC
            """
        ).fetchall()

    posts = []
    posts_by_id = {}
    for row in rows:
        entry = row_to_entry(row)
        if entry["parent_id"] is None:
            entry["replies"] = []
            posts.append(entry)
            posts_by_id[entry["id"]] = entry

    for row in rows:
        if row["parent_id"] is None:
            continue
        reply = row_to_entry(row)
        parent = posts_by_id.get(reply["parent_id"])
        if parent is not None:
            parent["replies"].append(reply)

    return list(reversed(posts))


def append_report(report):
    try:
        if REPORTS_FILE.exists():
            with REPORTS_FILE.open("r", encoding="utf-8") as file:
                reports = json.load(file)
            if not isinstance(reports, list):
                reports = []
        else:
            reports = []

        reports.append(report)
        with REPORTS_FILE.open("w", encoding="utf-8") as file:
            json.dump(reports, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise RuntimeError("Could not save report data.") from exc


def validate_payload(payload, require_profile=True):
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    content = str(payload.get("content", "")).strip()
    if not content:
        return None, "Content cannot be empty."
    if len(content) > MAX_CONTENT_LENGTH:
        return None, f"Content must be under {MAX_CONTENT_LENGTH} characters."

    alias = str(payload.get("alias", "")).strip() or DEFAULT_ALIAS
    if require_profile and not alias:
        return None, "Name is required."
    if len(alias) > MAX_ALIAS_LENGTH:
        return None, f"Name must be under {MAX_ALIAS_LENGTH} characters."

    avatar_url = str(payload.get("avatar") or payload.get("avatar_url") or DEFAULT_AVATAR_URL).strip()
    avatar_url = avatar_url or DEFAULT_AVATAR_URL
    identity_id, identity_error = identity_from_payload(payload)
    if identity_error:
        return None, identity_error

    return {
        "alias": alias,
        "avatar_url": avatar_url,
        "content": content,
        "session_id": identity_id,
    }, None


def insert_entry(validated, parent_id=None):
    created_at = current_timestamp()
    with get_db() as connection:
        if parent_id is not None:
            parent = connection.execute(
                "SELECT id FROM entries WHERE id = ? AND parent_id IS NULL",
                (parent_id,),
            ).fetchone()
            if parent is None:
                return None

        cursor = connection.execute(
            """
            INSERT INTO entries (content, parent_id, created_at, session_id, alias, avatar_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                validated["content"],
                parent_id,
                created_at,
                validated["session_id"],
                validated["alias"],
                validated["avatar_url"],
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT
                id,
                content,
                parent_id,
                created_at,
                session_id,
                alias,
                avatar_url,
                total_rating_stars,
                rating_count
            FROM entries
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

    entry = row_to_entry(row)
    if entry["parent_id"] is None:
        entry["replies"] = []
    return entry


def update_entry(entry_id, session_id, new_content):
    with get_db() as connection:
        row = connection.execute(
            "SELECT id, session_id FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None, "Thought not found."
        if not row["session_id"] or row["session_id"] != session_id:
            return None, "You can only edit your own thoughts."

        connection.execute(
            "UPDATE entries SET content = ? WHERE id = ?",
            (new_content, entry_id),
        )
        connection.commit()
        updated = connection.execute(
            """
            SELECT
                id,
                content,
                parent_id,
                created_at,
                session_id,
                alias,
                avatar_url,
                total_rating_stars,
                rating_count
            FROM entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
    return row_to_entry(updated), None


def delete_entry(entry_id, session_id):
    with get_db() as connection:
        row = connection.execute(
            "SELECT id, session_id FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return "Thought not found."
        if not row["session_id"] or row["session_id"] != session_id:
            return "You can only delete your own thoughts."

        connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        connection.commit()
    return None


def add_entry_rating(entry_id, stars, session_id):
    with get_db() as connection:
        row = connection.execute(
            """
            SELECT id, session_id, total_rating_stars, rating_count
            FROM entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        if row is None:
            return None, "Thought not found."
        if row["session_id"] and row["session_id"] == session_id:
            return None, "You cannot rate your own murmur."

        existing_rating = connection.execute(
            """
            SELECT stars
            FROM ratings
            WHERE post_id = ? AND session_id = ?
            """,
            (entry_id, session_id),
        ).fetchone()

        total_rating_stars = as_int(row["total_rating_stars"], 0)
        rating_count = as_int(row["rating_count"], 0)
        if existing_rating is None:
            connection.execute(
                """
                INSERT INTO ratings (post_id, session_id, stars)
                VALUES (?, ?, ?)
                """,
                (entry_id, session_id, stars),
            )
            total_rating_stars += stars
            rating_count += 1
        else:
            old_stars = as_int(existing_rating["stars"], 0)
            diff = stars - old_stars
            connection.execute(
                """
                UPDATE ratings
                SET stars = ?
                WHERE post_id = ? AND session_id = ?
                """,
                (stars, entry_id, session_id),
            )
            total_rating_stars += diff

        connection.execute(
            """
            UPDATE entries
            SET total_rating_stars = ?, rating_count = ?
            WHERE id = ?
            """,
            (total_rating_stars, rating_count, entry_id),
        )
        connection.commit()

    return {
        "post_id": entry_id,
        "average_rating": round(total_rating_stars / rating_count, 2) if rating_count else 0,
        "rating_count": rating_count,
    }, None


def broadcast_presence():
    socketio.emit("user_count_update", {"connected_users": connected_users})


def broadcast_typing_status():
    socketio.emit("typing_status_update", {"typing_count": len(typing_users)})


def normalize_report(payload):
    if not isinstance(payload, dict):
        return None, "Report body must be an object."

    report_type = str(payload.get("type", "")).strip()
    details = str(payload.get("details", "")).strip()
    timestamp = str(payload.get("timestamp") or current_timestamp()).strip()

    if report_type not in {"Spam", "Harassment", "Inappropriate Content"}:
        return None, "Choose a valid report type."
    if not details:
        return None, "Report details are required."

    return {
        "type": report_type,
        "details": details,
        "timestamp": timestamp,
        "received_at": current_timestamp(),
    }, None


@app.get("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/identity")
def get_identity():
    existing_token = str(request.headers.get("X-Murmur-Identity", "")).strip()
    identity_id = verify_identity_token(existing_token)
    if identity_id:
        return jsonify({"identity_token": existing_token, "identity_id": identity_id}), 200

    token, identity_id = issue_identity_token()
    return jsonify({"identity_token": token, "identity_id": identity_id}), 200


@app.get("/api/murmurs")
@app.get("/murmurs")
def get_murmurs():
    try:
        with storage_lock:
            return jsonify(load_entries()), 200
    except Exception:
        return jsonify({"error": "Could not retrieve murmurs."}), 500


@socketio.on("connect")
def handle_connect():
    global connected_users
    with presence_lock:
        connected_users += 1
        broadcast_presence()
        broadcast_typing_status()


@socketio.on("disconnect")
def handle_disconnect():
    global connected_users
    with presence_lock:
        connected_users = max(0, connected_users - 1)
        typing_users.discard(request.sid)
        broadcast_presence()
        broadcast_typing_status()


@socketio.on("typing_status")
def handle_typing_status(payload):
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Typing payload must be an object."}

    with presence_lock:
        if payload.get("is_typing"):
            typing_users.add(request.sid)
        else:
            typing_users.discard(request.sid)
        broadcast_typing_status()

    return {"ok": True, "typing_count": len(typing_users)}


def create_thought(payload):
    try:
        validated, error = validate_payload(payload)
        if error:
            emit("stream_error", {"error": error})
            return {"ok": False, "error": error}

        with storage_lock:
            murmur = insert_entry(validated)

        emit("broadcast_thought", murmur, broadcast=True)
        return {"ok": True, "thought": murmur}
    except Exception:
        error = "Could not create murmur."
        emit("stream_error", {"error": error})
        return {"ok": False, "error": error}


@socketio.on("submit_thought")
def handle_submit_thought(payload):
    return create_thought(payload)


@socketio.on("new_thought")
def handle_new_thought(payload):
    return create_thought(payload)


def create_reply(payload):
    try:
        validated, error = validate_payload(payload)
        if error:
            emit("stream_error", {"error": error})
            return {"ok": False, "error": error}

        murmur_id = as_int(payload.get("parent_id") or payload.get("murmur_id"), None)
        if murmur_id is None:
            error = "Parent murmur not found."
            emit("stream_error", {"error": error})
            return {"ok": False, "error": error}

        with storage_lock:
            reply = insert_entry(validated, murmur_id)
            if reply is None:
                error = "Parent murmur not found."
                emit("stream_error", {"error": error})
                return {"ok": False, "error": error}

        emit("broadcast_reply", reply, broadcast=True)
        return {"ok": True, "reply": reply}
    except Exception:
        error = "Could not create reply."
        emit("stream_error", {"error": error})
        return {"ok": False, "error": error}


@socketio.on("new_reply")
def handle_new_reply(payload):
    return create_reply(payload)


@socketio.on("delete_thought")
def handle_delete_thought(payload):
    try:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Request body must be a JSON object."}

        thought_id = as_int(payload.get("id"), None)
        identity_id, identity_error = identity_from_payload(payload)
        if thought_id is None:
            return {"ok": False, "error": "Thought id is required."}
        if identity_error:
            return {"ok": False, "error": identity_error}

        with storage_lock:
            error = delete_entry(thought_id, identity_id)
        if error:
            emit("stream_error", {"error": error})
            return {"ok": False, "error": error}

        emit("thought_deleted", {"id": thought_id}, broadcast=True)
        return {"ok": True, "id": thought_id}
    except Exception:
        error = "Could not delete thought."
        emit("stream_error", {"error": error})
        return {"ok": False, "error": error}


@socketio.on("edit_thought")
def handle_edit_thought(payload):
    try:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Request body must be a JSON object."}

        thought_id = as_int(payload.get("id"), None)
        new_content = str(payload.get("new_content", "")).strip()
        identity_id, identity_error = identity_from_payload(payload)
        if thought_id is None:
            return {"ok": False, "error": "Thought id is required."}
        if identity_error:
            return {"ok": False, "error": identity_error}
        if not new_content:
            return {"ok": False, "error": "Content cannot be empty."}
        if len(new_content) > MAX_CONTENT_LENGTH:
            return {"ok": False, "error": f"Content must be under {MAX_CONTENT_LENGTH} characters."}

        with storage_lock:
            entry, error = update_entry(thought_id, identity_id, new_content)
        if error:
            emit("stream_error", {"error": error})
            return {"ok": False, "error": error}

        emit(
            "thought_edited",
            {"id": thought_id, "new_content": entry["content"]},
            broadcast=True,
        )
        return {"ok": True, "thought": entry}
    except Exception:
        error = "Could not edit thought."
        emit("stream_error", {"error": error})
        return {"ok": False, "error": error}


@socketio.on("submit_rating")
def handle_submit_rating(payload):
    try:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Rating payload must be an object."}

        post_id = as_int(payload.get("post_id"), None)
        stars = as_int(payload.get("stars"), None)
        identity_id, identity_error = identity_from_payload(payload)
        if post_id is None:
            return {"ok": False, "error": "Post id is required."}
        if stars not in {1, 2, 3, 4, 5}:
            return {"ok": False, "error": "Rating must be between 1 and 5 stars."}
        if identity_error:
            return {"ok": False, "error": identity_error}

        with storage_lock:
            rating, error = add_entry_rating(post_id, stars, identity_id)
        if error:
            emit("rating_error", {"error": error})
            return {"ok": False, "error": error}

        emit("rating_updated", rating, broadcast=True)
        return {"ok": True, "rating": rating}
    except Exception:
        error = "Could not save rating."
        emit("stream_error", {"error": error})
        return {"ok": False, "error": error}


@socketio.on("submit_report")
def handle_submit_report(data):
    try:
        report, error = normalize_report(data)
        if error:
            emit("report_status", {"ok": False, "error": error})
            return {"ok": False, "error": error}

        print(f"🚨 REPORT RECEIVED: {report}", flush=True)
        with report_lock:
            append_report(report)

        emit("report_status", {"ok": True, "message": "Report received."})
        return {"ok": True, "report": report}
    except Exception:
        error = "Could not save report."
        emit("report_status", {"ok": False, "error": error})
        return {"ok": False, "error": error}


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed."}), 405


initialize_database()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
