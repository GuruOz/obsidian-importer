#!/usr/bin/env python3
"""Persistent chat web UI over the vault - the NotebookLM-style always-on
counterpart to ask_vault.py's one-shot CLI. Read-only: the tool set never
includes write_file/edit_file, regardless of DRY_RUN or any request input.

Run directly (used as the container's command, see docker-compose.yml):
    python3 scripts/vault_web.py
"""
import json
import os
import queue
import sys
import threading
import time
import uuid
import subprocess

from flask import Flask, Response, jsonify, request, send_from_directory

import custom_agent_loop as cal
import lexical_index
import session_store as store
import vault_qa

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("VAULT_QA_PORT", "8420"))
MAX_TURNS = int(os.environ.get("VAULT_QA_MAX_TURNS", "20"))
MAX_SESSIONS = int(os.environ.get("VAULT_QA_MAX_SESSIONS", "50"))
KEEPALIVE_SECONDS = 15

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# Live-session cache only: the durable copy of every chat lives in
# session_store (SQLite). Evicting from here or restarting the container
# loses nothing - the session is revived from the store on next use.
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


@app.before_request
def _check_auth():
    # Single hook point for future auth (e.g. a shared-secret VAULT_QA_TOKEN header
    # check). Left as a no-op for now - this UI is LAN-only, same posture as the
    # existing Obsidian GUI.
    pass


def _new_session():
    index = vault_qa.build_vault_index()
    lex = lexical_index.build_index(cal.VAULT_DIR)
    system_content = vault_qa.build_system_prompt(index, lexical_index=lex)
    return {
        "messages": [{"role": "system", "content": system_content}],
        # Display-layer history for the sidebar/re-hydration endpoints: a list of
        # {"type": "user", "text": ...} and {"type": "answer", ...answer payload}
        # entries. Unlike "messages" it is never capped and holds no tool chatter.
        "transcript": [],
        "title": None,
        "created": time.time(),
        "lexical_index": lex,
        "note_count": len(index),
        "chunk_count": len(lex.chunks),
        "lock": threading.Lock(),
        "last_used": time.time(),
    }


def _revive_session(row):
    """Rebuild a live session from a stored one. The model-facing history is
    replayed from the display transcript (user turns + answer text), so
    tool-call chatter from before the restart is dropped - the fresh system
    prompt and vault index are rebuilt anyway."""
    session = _new_session()
    session["transcript"] = list(row["transcript"])
    session["title"] = row["title"]
    session["created"] = row["created"]
    session["last_used"] = row["last_used"]
    for e in row["transcript"]:
        if e.get("type") == "user":
            session["messages"].append({"role": "user", "content": e.get("text") or ""})
        elif e.get("type") == "answer":
            session["messages"].append({"role": "assistant", "content": e.get("answer") or ""})
    _cap_history(session["messages"], MAX_TURNS)
    return session


def get_or_create_session(session_id):
    with SESSIONS_LOCK:
        if session_id and session_id in SESSIONS:
            return session_id, SESSIONS[session_id]
    # Building the vault/lexical indexes is slow; do it outside SESSIONS_LOCK.
    row = store.load(session_id) if session_id else None
    fresh = _revive_session(row) if row else _new_session()
    with SESSIONS_LOCK:
        if session_id and session_id in SESSIONS:  # lost a revive race; use the winner
            return session_id, SESSIONS[session_id]
        new_id = session_id or str(uuid.uuid4())
        if len(SESSIONS) >= MAX_SESSIONS:
            # Cache eviction only - the chat stays in the store and is revived
            # on next use. A worker mid-request keeps its own reference and
            # still persists its answer.
            oldest = min(SESSIONS, key=lambda k: SESSIONS[k]["last_used"])
            del SESSIONS[oldest]
        SESSIONS[new_id] = fresh
        return new_id, fresh


def _persist(session_id, session):
    store.save(session_id, session["title"], session["created"],
               session["last_used"], session["transcript"])


def _role(m):
    """Message role, whether m is a plain dict or an OpenAI SDK message object
    (run_loop appends the SDK's assistant-message objects to the history)."""
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)


def _cap_history(messages, max_turns):
    """Keep messages[0] (system) plus at most the last max_turns user turns."""
    user_idxs = [i for i, m in enumerate(messages) if _role(m) == "user"]
    if len(user_idxs) <= max_turns:
        return
    cutoff = user_idxs[-max_turns]
    del messages[1:cutoff]


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


HOST_DIR = "/host" if os.path.exists("/host") else "."
EDITABLE_FILES = [
    ".env",
    "crontab",
    "prompt_template.txt",
    "prompt_dry_run.txt",
    "prompt_vault_profile.txt",
    "prompt_weekly_rollup.txt",
    "prompt_personal_email.txt",
    "prompt_personal_email_dry_run.txt"
]

@app.route("/api/settings")
def list_settings():
    return jsonify({"files": EDITABLE_FILES})

@app.route("/api/settings/<filename>", methods=["GET", "PUT"])
def manage_setting(filename):
    if filename not in EDITABLE_FILES:
        return jsonify({"error": "invalid file"}), 400
    filepath = os.path.join(HOST_DIR, filename)
    
    if request.method == "GET":
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            if filename == ".env":
                lines = []
                for line in content.splitlines():
                    if not line.strip().startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        lines.append({"type": "kv", "key": k.strip(), "value": v.strip(), "raw": line})
                    else:
                        lines.append({"type": "comment", "raw": line})
                return jsonify({"filename": filename, "type": "env", "fields": lines})
            else:
                return jsonify({"filename": filename, "type": "text", "content": content})
        except FileNotFoundError:
            return jsonify({"error": "file not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if request.method == "PUT":
        body = request.get_json(force=True, silent=True) or {}
        if filename == ".env":
            updates = body.get("updates", {})
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    old_lines = f.read().splitlines()
                new_lines = []
                for line in old_lines:
                    if not line.strip().startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        if k in updates:
                            new_lines.append(f"{k}={updates[k]}")
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_lines) + "\n")
                return jsonify({"ok": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        else:
            content = body.get("content")
            if content is None:
                return jsonify({"error": "content required"}), 400
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                return jsonify({"ok": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

@app.route("/api/system/restart", methods=["POST"])
def system_restart():
    try:
        subprocess.Popen([
            "curl", "-s", "--unix-socket", "/var/run/docker.sock",
            "-X", "POST", "http://localhost/containers/copilot-digest-pipeline/restart"
        ])
        subprocess.Popen([
            "curl", "-s", "--unix-socket", "/var/run/docker.sock",
            "-X", "POST", "http://localhost/containers/copilot-digest-vault-qa/restart"
        ])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/meta")
def meta():
    return jsonify({
        "vault_name": os.path.basename(cal.VAULT_DIR),
        "notes": len(vault_qa.build_vault_index()),
    })


@app.route("/api/sessions")
def list_sessions():
    query = (request.args.get("q") or "").strip()
    return jsonify({"sessions": store.list_all(query or None)})


@app.route("/api/sessions/<sid>")
def get_session(sid):
    row = store.load(sid)
    if row is None:
        return jsonify({"error": "unknown session"}), 404
    out = {"id": sid, "title": row["title"] or "New chat", "created": row["created"],
           "last_used": row["last_used"], "turns": row["turns"],
           "transcript": row["transcript"]}
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid)
        if s is not None:
            # Per-session index counts only exist while the session is live;
            # the client falls back to the vault-wide /api/meta count otherwise.
            out["notes"] = s["note_count"]
            out["chunks"] = s["chunk_count"]
    return jsonify(out)


@app.route("/api/sessions/<sid>", methods=["PATCH"])
def update_session(sid):
    body = request.get_json(force=True, silent=True) or {}
    out = {"id": sid}
    if "title" in body:
        title = (body.get("title") or "").strip()[:120]
        if not title:
            return jsonify({"error": "empty title"}), 400
        if not store.rename(sid, title):
            return jsonify({"error": "unknown session"}), 404
        with SESSIONS_LOCK:
            if sid in SESSIONS:
                SESSIONS[sid]["title"] = title
        out["title"] = title
    if "pinned" in body:
        pinned = bool(body.get("pinned"))
        if not store.set_pinned(sid, pinned):
            return jsonify({"error": "unknown session"}), 404
        out["pinned"] = pinned
    if len(out) == 1:
        return jsonify({"error": "nothing to update"}), 400
    return jsonify(out)


@app.route("/api/sessions/<sid>/feedback", methods=["POST"])
def feedback(sid):
    """Thumbs up/down on the Nth answer of a session (rating null clears it).
    Stored on the transcript's answer entry, so it persists, hydrates with the
    chat, and dies with a regenerated answer."""
    body = request.get_json(force=True, silent=True) or {}
    idx = body.get("answer_index")
    rating = body.get("rating")
    if not isinstance(idx, int) or isinstance(idx, bool) or rating not in ("up", "down", None):
        return jsonify({"error": "invalid feedback"}), 400

    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is not None:
        transcript, row = session["transcript"], None
    else:
        row = store.load(sid)
        if row is None:
            return jsonify({"error": "unknown session"}), 404
        transcript = row["transcript"]

    answers = [e for e in transcript if e.get("type") == "answer"]
    if not 0 <= idx < len(answers):
        return jsonify({"error": "no such answer"}), 400
    if rating is None:
        answers[idx].pop("feedback", None)
    else:
        answers[idx]["feedback"] = rating
    if session is not None:
        _persist(sid, session)
    else:
        store.save(sid, row["title"], row["created"], row["last_used"], transcript)
    print(f"feedback: session={sid} answer={idx} rating={rating}", file=sys.stderr)
    return jsonify({"ok": True, "answer_index": idx, "rating": rating})


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def delete_session(sid):
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid)
        if s is not None:
            if not s["lock"].acquire(blocking=False):
                return jsonify({"error": "a request is in flight for this session"}), 409
            del SESSIONS[sid]
            s["lock"].release()
    if not store.delete(sid) and s is None:
        return jsonify({"error": "unknown session"}), 404
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True, silent=True) or {}
    user_message = (body.get("message") or "").strip()
    regenerate = bool(body.get("regenerate"))
    if not user_message and not regenerate:
        return jsonify({"error": "empty message"}), 400

    session_id, session = get_or_create_session(body.get("session_id"))
    if not session["lock"].acquire(blocking=False):
        return jsonify({"error": "a request is already in flight for this session"}), 409

    q = queue.Queue()
    # Fresh per request, so a stale stop for a finished request can't cancel
    # this one. /api/chat/stop sets it; run_loop checks it between stream
    # chunks and tool dispatches.
    cancel = threading.Event()
    session["cancel"] = cancel

    def progress_cb(tool_name, args):
        if tool_name == "finish":
            return  # the answer event follows immediately; don't flash it as progress
        q.put(("progress", {"tool": tool_name, "args": args}))

    def stream_cb(text):
        q.put(("delta", {"text": text}))

    def worker():
        t0 = time.time()
        usage = {}

        def emit_answer(payload):
            payload["usage"] = usage
            payload["elapsed"] = round(time.time() - t0, 1)
            session["transcript"].append({"type": "answer", **payload})
            session["last_used"] = time.time()
            _persist(session_id, session)
            q.put(("answer", payload))

        try:
            tools = vault_qa.build_tools(lexical_index=session["lexical_index"])
            handlers = vault_qa.build_handlers(lexical_index=session["lexical_index"])
            finish_args = cal.run_loop(CLIENT, MODEL, session["messages"], tools, handlers,
                                        max_loops=40, progress_cb=progress_cb,
                                        usage_out=usage, stream_cb=stream_cb,
                                        cancel_event=cancel)
            if finish_args is None:
                q.put(("error", {"message": "Exceeded maximum tool call loops"}))
            else:
                emit_answer(dict(finish_args))
        except cal.AgentCancelled as e:
            # Solidify whatever streamed before the stop as a stored answer.
            emit_answer({"answer": e.partial_answer or "", "stopped": True})
        except cal.AgentAPIError as e:
            q.put(("error", {"message": str(e)}))
        except Exception as e:
            q.put(("error", {"message": f"Unexpected error: {e}"}))
        finally:
            session["last_used"] = time.time()
            session["lock"].release()
            q.put(("__done__", {}))

    # Anything that fails between acquiring the lock and the worker taking
    # ownership of it must release it, or the session is bricked with 409s.
    try:
        if regenerate:
            # Rewind to just after the last user turn and re-run it, instead of
            # appending a duplicate user message.
            user_idxs = [i for i, m in enumerate(session["messages"]) if _role(m) == "user"]
            if not user_idxs:
                session["lock"].release()
                return jsonify({"error": "nothing to regenerate in this session"}), 400
            del session["messages"][user_idxs[-1] + 1:]
            t_idxs = [i for i, e in enumerate(session["transcript"]) if e["type"] == "user"]
            if t_idxs:
                del session["transcript"][t_idxs[-1] + 1:]
            session["last_used"] = time.time()
            _persist(session_id, session)
        else:
            session["messages"].append({"role": "user", "content": user_message})
            _cap_history(session["messages"], MAX_TURNS)
            session["transcript"].append({"type": "user", "text": user_message})
            if session["title"] is None:
                session["title"] = " ".join(user_message.split())[:80]
            session["last_used"] = time.time()
            _persist(session_id, session)
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        session["lock"].release()
        raise

    def stream():
        session_evt = {"session_id": session_id,
                       "notes": session["note_count"], "chunks": session["chunk_count"]}
        yield f"event: session\ndata: {json.dumps(session_evt)}\n\n"
        while True:
            try:
                event, payload = q.get(timeout=KEEPALIVE_SECONDS)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if event == "__done__":
                break
            yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/chat/stop", methods=["POST"])
def chat_stop():
    """Cancel the in-flight request for a session. The worker aborts at the
    next stream-chunk/tool boundary and emits a stopped answer carrying the
    partial text, so the client's normal answer path solidifies it."""
    body = request.get_json(force=True, silent=True) or {}
    sid = body.get("session_id")
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        return jsonify({"error": "unknown session"}), 404
    cancel = session.get("cancel")
    if cancel is None or not session["lock"].locked():
        return jsonify({"error": "no request in flight"}), 409
    cancel.set()
    return jsonify({"ok": True})


def _init_client():
    global CLIENT, MODEL
    try:
        CLIENT, MODEL = cal.make_client()
    except cal.ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


_init_client()
store.init()


if __name__ == "__main__":
    from waitress import serve
    print(f"Starting vault chat UI on :{PORT} (model {MODEL})...", file=sys.stderr)
    serve(app, host="0.0.0.0", port=PORT, threads=4)
