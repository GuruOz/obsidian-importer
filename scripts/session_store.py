"""Durable storage for vault-qa chat sessions (stdlib sqlite3, no new deps).

vault_web.py keeps live sessions in memory only as a working cache; every
mutation is written through here, so chats survive container restarts and
cache eviction. The transcript is the display-layer history (user turns +
answer payloads) - the model-facing message list is rebuilt from it on
revival, which intentionally drops tool-call chatter.

The DB lives outside the vault (the vault-qa container mounts the vault
read-only); see docker-compose.yml, which mounts ./data/vault-qa at /data.
"""
import json
import os
import sqlite3

DB_PATH = os.environ.get("VAULT_QA_DB", "/data/sessions.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    created    REAL NOT NULL,
    last_used  REAL NOT NULL,
    turns      INTEGER NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '[]'
)
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect() as conn:
        conn.execute(_SCHEMA)


def save(sid, title, created, last_used, transcript):
    turns = sum(1 for e in transcript if e.get("type") == "user")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created, last_used, turns, transcript)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
            " last_used=excluded.last_used, turns=excluded.turns,"
            " transcript=excluded.transcript",
            (sid, title, created, last_used, turns, json.dumps(transcript)))


def list_all():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created, last_used, turns FROM sessions"
            " ORDER BY last_used DESC").fetchall()
    return [{"id": r[0], "title": r[1] or "New chat", "created": r[2],
             "last_used": r[3], "turns": r[4]} for r in rows]


def load(sid):
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, title, created, last_used, turns, transcript"
            " FROM sessions WHERE id = ?", (sid,)).fetchone()
    if r is None:
        return None
    return {"id": r[0], "title": r[1], "created": r[2], "last_used": r[3],
            "turns": r[4], "transcript": json.loads(r[5])}


def rename(sid, title):
    with _connect() as conn:
        cur = conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
        return cur.rowcount > 0


def delete(sid):
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        return cur.rowcount > 0
