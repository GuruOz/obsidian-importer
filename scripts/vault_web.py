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

from flask import Flask, Response, jsonify, request, send_from_directory

import custom_agent_loop as cal
import lexical_index
import vault_qa

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("VAULT_QA_PORT", "8420"))
MAX_TURNS = int(os.environ.get("VAULT_QA_MAX_TURNS", "20"))
MAX_SESSIONS = int(os.environ.get("VAULT_QA_MAX_SESSIONS", "50"))
KEEPALIVE_SECONDS = 15

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

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
        "lexical_index": lex,
        "note_count": len(index),
        "chunk_count": len(lex.chunks),
        "lock": threading.Lock(),
        "last_used": time.time(),
    }


def get_or_create_session(session_id):
    with SESSIONS_LOCK:
        if session_id and session_id in SESSIONS:
            return session_id, SESSIONS[session_id]
        new_id = session_id or str(uuid.uuid4())
        if len(SESSIONS) >= MAX_SESSIONS:
            oldest = min(SESSIONS, key=lambda k: SESSIONS[k]["last_used"])
            del SESSIONS[oldest]
        SESSIONS[new_id] = _new_session()
        return new_id, SESSIONS[new_id]


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


@app.route("/api/meta")
def meta():
    return jsonify({
        "vault_name": os.path.basename(cal.VAULT_DIR),
        "notes": len(vault_qa.build_vault_index()),
    })


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

    def progress_cb(tool_name, args):
        if tool_name == "finish":
            return  # the answer event follows immediately; don't flash it as progress
        q.put(("progress", {"tool": tool_name, "args": args}))

    def worker():
        t0 = time.time()
        usage = {}
        try:
            tools = vault_qa.build_tools(lexical_index=session["lexical_index"])
            handlers = vault_qa.build_handlers(lexical_index=session["lexical_index"])
            finish_args = cal.run_loop(CLIENT, MODEL, session["messages"], tools, handlers,
                                        max_loops=40, progress_cb=progress_cb,
                                        usage_out=usage)
            if finish_args is None:
                q.put(("error", {"message": "Exceeded maximum tool call loops"}))
            else:
                payload = dict(finish_args)
                payload["usage"] = usage
                payload["elapsed"] = round(time.time() - t0, 1)
                q.put(("answer", payload))
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
        else:
            session["messages"].append({"role": "user", "content": user_message})
            _cap_history(session["messages"], MAX_TURNS)
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


def _init_client():
    global CLIENT, MODEL
    try:
        CLIENT, MODEL = cal.make_client()
    except cal.ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


_init_client()


if __name__ == "__main__":
    from waitress import serve
    print(f"Starting vault chat UI on :{PORT} (model {MODEL})...", file=sys.stderr)
    serve(app, host="0.0.0.0", port=PORT, threads=4)
