from datetime import datetime
from pathlib import Path
import json
import os
import sys
from threading import Lock


BASE_DIR = Path(__file__).resolve().parent
VENDOR_PATH = BASE_DIR / "vendor"
DATA_FILE = BASE_DIR / "murmurs.json"
MAX_CONTENT_LENGTH = 500
DEFAULT_ALIAS = "Anonymous"

if VENDOR_PATH.exists():
    sys.path.insert(0, str(VENDOR_PATH))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS


app = Flask(__name__)
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
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization", "Accept"],
        }
    },
)

storage_lock = Lock()
murmurs = []


def current_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def as_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def normalize_reply(item, fallback_id, parent_id):
    return {
        "id": as_int(item.get("id"), fallback_id),
        "parent_id": parent_id,
        "alias": str(item.get("alias") or DEFAULT_ALIAS).strip() or DEFAULT_ALIAS,
        "content": str(item.get("content") or item.get("text") or ""),
        "created_at": str(item.get("created_at") or item.get("timestamp") or current_timestamp()),
    }


def normalize_murmur(item, fallback_id):
    murmur_id = as_int(item.get("id"), fallback_id)
    replies = item.get("replies") or []

    return {
        "id": murmur_id,
        "alias": str(item.get("alias") or DEFAULT_ALIAS).strip() or DEFAULT_ALIAS,
        "content": str(item.get("content") or item.get("text") or ""),
        "created_at": str(item.get("created_at") or item.get("timestamp") or current_timestamp()),
        "replies": [
            normalize_reply(reply, index + 1, murmur_id)
            for index, reply in enumerate(replies)
            if isinstance(reply, dict)
        ],
    }


def load_murmurs():
    try:
        if not DATA_FILE.exists():
            return []

        with DATA_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            return []

        normalized = []
        for index, item in enumerate(data):
            if isinstance(item, dict):
                normalized.append(normalize_murmur(item, index + 1))

        return normalized
    except Exception:
        return []


def save_murmurs():
    try:
        with DATA_FILE.open("w", encoding="utf-8") as file:
            json.dump(murmurs, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise RuntimeError("Could not save murmur data.") from exc


def validate_payload(payload):
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    content = str(payload.get("content", "")).strip()
    if not content:
        return None, "Content cannot be empty."
    if len(content) > MAX_CONTENT_LENGTH:
        return None, f"Content must be under {MAX_CONTENT_LENGTH} characters."

    alias = str(payload.get("alias", DEFAULT_ALIAS)).strip() or DEFAULT_ALIAS
    return {"alias": alias, "content": content}, None


def next_murmur_id():
    return max((as_int(item.get("id")) for item in murmurs), default=0) + 1


def next_reply_id(murmur):
    return max((as_int(item.get("id")) for item in murmur.get("replies", [])), default=0) + 1


def find_murmur(murmur_id):
    return next((item for item in murmurs if item.get("id") == murmur_id), None)


@app.get("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/murmurs")
@app.get("/murmurs")
def get_murmurs():
    try:
        with storage_lock:
            return jsonify(list(reversed(murmurs))), 200
    except Exception:
        return jsonify({"error": "Could not retrieve murmurs."}), 500


@app.post("/api/murmurs")
@app.post("/murmurs")
def create_murmur():
    try:
        payload = request.get_json(silent=True)
        validated, error = validate_payload(payload)

        if error:
            return jsonify({"error": error}), 400

        murmur = {
            "id": next_murmur_id(),
            "alias": validated["alias"],
            "content": validated["content"],
            "created_at": current_timestamp(),
            "replies": [],
        }

        with storage_lock:
            murmurs.append(murmur)
            save_murmurs()

        return jsonify(murmur), 201
    except Exception:
        return jsonify({"error": "Could not create murmur."}), 500


@app.post("/api/murmurs/<int:murmur_id>/replies")
@app.post("/murmurs/<int:murmur_id>/replies")
def create_reply(murmur_id):
    try:
        payload = request.get_json(silent=True)
        validated, error = validate_payload(payload)

        if error:
            return jsonify({"error": error}), 400

        with storage_lock:
            parent = find_murmur(murmur_id)
            if parent is None:
                return jsonify({"error": "Parent murmur not found."}), 404

            reply = {
                "id": next_reply_id(parent),
                "parent_id": murmur_id,
                "alias": validated["alias"],
                "content": validated["content"],
                "created_at": current_timestamp(),
            }
            parent.setdefault("replies", []).append(reply)
            save_murmurs()

        return jsonify(reply), 201
    except Exception:
        return jsonify({"error": "Could not create reply."}), 500


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed."}), 405


murmurs = load_murmurs()


if __name__ == "__main__":
    host = os.getenv("MURMUR_HOST", "127.0.0.1")
    port = int(os.getenv("MURMUR_PORT", "5000"))
    debug = os.getenv("MURMUR_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
