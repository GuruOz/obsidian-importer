#!/usr/bin/env python3
"""Persistent chat web UI over the vault - the NotebookLM-style always-on
counterpart to ask_vault.py's one-shot CLI. Read-only: the tool set never
includes write_file/edit_file, regardless of DRY_RUN or any request input.

Run directly (used as the container's command, see docker-compose.yml):
    python3 scripts/vault_web.py
"""
import difflib
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
import subprocess
import tempfile
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory

import custom_agent_loop as cal
import lexical_index
import semantic_index
import session_store as store
import vault_qa
from tzutil import APP_TZ

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


# Cache the (index, searcher) across sessions so a new chat doesn't re-walk and
# re-tokenize the whole vault every time. Invalidated by a cheap stat signature
# (note count + newest mtime), so vault edits made outside this process are picked
# up without ever serving a stale index. The searcher's semantic layer keeps
# syncing in the background against the same cached object.
_INDEX_CACHE = {"sig": None, "index": None, "searcher": None}
_INDEX_CACHE_LOCK = threading.Lock()


def _vault_signature():
    count, max_mtime = 0, 0.0
    for root, dirs, files in os.walk(cal.VAULT_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("Attachments", "smart-chats")]
        for f in files:
            if f.endswith(".md"):
                count += 1
                try:
                    max_mtime = max(max_mtime, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
    return (count, max_mtime)


def _get_search_context():
    """(index, searcher) for a session, reused across sessions until the vault
    changes. Building the indexes is slow; this makes new-chat latency the cost
    of a stat walk when nothing changed."""
    sig = _vault_signature()
    with _INDEX_CACHE_LOCK:
        if _INDEX_CACHE["sig"] == sig and _INDEX_CACHE["searcher"] is not None:
            return _INDEX_CACHE["index"], _INDEX_CACHE["searcher"]
    index = vault_qa.build_vault_index()
    searcher = semantic_index.build_searcher(cal.VAULT_DIR)
    with _INDEX_CACHE_LOCK:
        _INDEX_CACHE.update(sig=sig, index=index, searcher=searcher)
    return index, searcher


def _new_session():
    index, lex = _get_search_context()
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


# --- Citation verification -------------------------------------------------
# The model self-reports citations (note path + verbatim snippet). We check each
# snippet actually appears in the cited note and annotate c["verified"]; we never
# drop or rewrite a citation. False negatives are expected (paraphrase, snippet
# cut mid-word at the 300-char cap, a fact synthesized across two chunks), so the
# UI wording is "couldn't locate this exact text", not "wrong".
_PUNCT_MAP = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "–": "-", "—": "-", " ": " ", "…": "...",
})
_VERIFY_SCAN_CAP = 200_000  # chars scanned per fuzzy check


def _normalize_text(s):
    s = (s or "").translate(_PUNCT_MAP).casefold()
    # Strip markdown decoration and quotes so their presence/absence (curly vs
    # straight, or a note quoting a phrase the model paraphrased unquoted) never
    # decides a match.
    s = re.sub(r"""[*_`#>\[\]"']""", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _read_full(path):
    """Resolve a cited path (as-is, +.md, or by basename) and return its full
    text, or None. Bypasses read_file's size cap so long notes verify correctly."""
    path = (path or "").replace("\\", "/").strip("/")
    if not path:
        return None
    candidates = [path]
    if not path.endswith(".md"):
        candidates.append(path + ".md")
    for cand in dict.fromkeys(candidates):
        try:
            p = cal.safe_path(cand)
        except Exception:
            continue
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except OSError:
                pass
    base = os.path.basename(path)
    if not base.endswith(".md"):
        base += ".md"
    index, _ = _get_search_context()
    for rel, _m in index:
        if os.path.basename(rel) == base:
            try:
                with open(os.path.join(cal.VAULT_DIR, rel), encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except OSError:
                pass
    return None


def _snippet_verified(snippet, content):
    ns = _normalize_text(snippet)
    if len(ns) < 8:
        return True                        # too short to meaningfully verify
    nc = _normalize_text(content)
    if ns in nc:
        return True
    # Fuzzy fallback for a snippet cut mid-word at the 300-char cap or with a
    # small transcription slip: the longest contiguous run shared with the note
    # must cover most of the snippet. Contiguity keeps this strict enough that a
    # genuinely paraphrased/hallucinated snippet fails.
    nc = nc[:_VERIFY_SCAN_CAP]
    sm = difflib.SequenceMatcher(None, ns, nc, autojunk=False)
    m = sm.find_longest_match(0, len(ns), 0, len(nc))
    return m.size / len(ns) >= 0.85


def _verify_citations(citations):
    if not citations or not isinstance(citations, list):
        return
    cache = {}
    for c in citations:
        if not isinstance(c, dict):
            continue
        path = (c.get("path") or "").strip()
        if not path:
            c["verified"] = False
            continue
        if path not in cache:
            cache[path] = _read_full(path) or ""
        content = cache[path]
        c["verified"] = bool(content) and _snippet_verified(c.get("snippet"), content)


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
    "prompt_personal_email_dry_run.txt",
    "prompt_telegram.txt",
    "prompt_telegram_dry_run.txt",
    "prompt_whatsapp.txt",
    "prompt_whatsapp_dry_run.txt"
]

PROPOSED_FILES = [
    "data/staging/proposed.md",
    "data/staging/personal/proposed.md",
    "data/staging/telegram/proposed.md",
    "data/staging/whatsapp/proposed.md"
]

@app.route("/api/settings")
def list_settings():
    return jsonify({"files": EDITABLE_FILES, "proposed": PROPOSED_FILES})

@app.route("/api/settings/<path:filename>", methods=["GET", "PUT"])
def manage_setting(filename):
    if filename not in EDITABLE_FILES and filename not in PROPOSED_FILES:
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
                seen_keys = set()
                for line in old_lines:
                    if not line.strip().startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        seen_keys.add(k)
                        if k in updates:
                            new_lines.append(f"{k}={updates[k]}")
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)
                # Keys not present as an uncommented line (e.g. settings that ship
                # commented-out in .env.example) would otherwise be dropped
                # silently; append them so saving a new setting actually sticks.
                missing = [k for k in updates if k not in seen_keys]
                if missing:
                    new_lines.append("")
                    new_lines.append("# Added via settings UI")
                    for k in missing:
                        new_lines.append(f"{k}={updates[k]}")
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

# Pipeline logs live at /work/logs in the pipeline container = ./data/logs on
# the host = /host/data/logs here (the project root is mounted at /host).
LOGS_DIR = os.path.join(HOST_DIR, "data", "logs")
LOG_TAIL_DEFAULT = 500
LOG_TAIL_MAX = 5000


@app.route("/api/logs")
def list_logs():
    """List pipeline log files, newest-modified first."""
    entries = []
    try:
        names = os.listdir(LOGS_DIR)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(LOGS_DIR, name)
        if not os.path.isfile(path):
            continue
        st = os.stat(path)
        entries.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return jsonify({"files": entries})


@app.route("/api/logs/<name>")
def read_log(name):
    # The log dir is flat; rejecting separators (Flask already blocks "/") and
    # dot-names pins reads inside it without realpath gymnastics.
    if "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"error": "invalid log name"}), 400
    path = os.path.join(LOGS_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"error": "log not found"}), 404
    try:
        lines = min(int(request.args.get("lines", LOG_TAIL_DEFAULT)), LOG_TAIL_MAX)
    except ValueError:
        lines = LOG_TAIL_DEFAULT
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    tail = all_lines[-lines:] if lines > 0 else all_lines
    return jsonify({
        "name": name,
        "total_lines": len(all_lines),
        "shown_lines": len(tail),
        "content": "".join(tail),
    })


DOCKER_SOCK = "/var/run/docker.sock"
PIPELINE_CONTAINER = "copilot-digest-pipeline"
INGEST_LOG_CAP = 200_000  # chars kept in the in-memory run log
INGEST_MAX_PASSES = 20    # drain-mode safety cap (25 emails/pass -> 500 emails)

# The two ingestion sources the UI can run. Both invoke run-ingest.sh <source>;
# they differ only in options (personal: lookback/window/drain; digest: work-date)
# and default pass count (a digest is one email/day, so draining is a no-op).
INGEST_SOURCES = {
    "personal": {"title": "Personal Email"},
    "digest": {"title": "Work Digest"},
    "telegram": {"title": "Telegram"},
    "whatsapp": {"title": "WhatsApp"},
}

# Single manual-ingestion run at a time, across BOTH sources - run-ingest.sh
# serializes them on one vault flock anyway, so a second concurrent web run would
# just block on the lock. State survives page reloads (the UI re-attaches via the
# status endpoint) but not a vault-qa restart - the run itself lives in the
# pipeline container and finishes either way; only the log view is lost.
INGEST_LOCK = threading.Lock()
INGEST_STATE = {
    "running": False, "source": None, "log": "", "exit_code": None,
    "passes": 0, "started": None, "finished": None,
    "stop_requested": False, "blocked": False,
}


def _docker_api(method, path, payload=None, stream=False):
    """Talk to the Docker Engine API over the mounted socket via curl (same
    transport as /api/system/restart). Returns a CompletedProcess, or a Popen
    with piped stdout when stream=True."""
    cmd = ["curl", "-sN", "--unix-socket", DOCKER_SOCK, "-X", method]
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(payload)]
    cmd.append(f"http://localhost{path}")
    if stream:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    return subprocess.run(cmd, capture_output=True, text=True)


def _ingest_append(text):
    with INGEST_LOCK:
        log = INGEST_STATE["log"] + text
        if len(log) > INGEST_LOG_CAP:
            log = log[-INGEST_LOG_CAP:]
        INGEST_STATE["log"] = log


def _run_ingest_pass(source, env_overrides):
    """One `run-ingest.sh <source>` execution inside the pipeline container.
    Streams its output into INGEST_STATE["log"]; returns (exit_code, output)."""
    create = _docker_api("POST", f"/containers/{PIPELINE_CONTAINER}/exec", {
        "AttachStdout": True, "AttachStderr": True,
        # Tty gives one raw merged stream instead of Docker's 8-byte-framed
        # multiplex, which curl can't unframe.
        "Tty": True,
        "Env": env_overrides,
        "Cmd": ["/app/scripts/run-ingest.sh", source],
    })
    try:
        exec_id = json.loads(create.stdout).get("Id")
    except (ValueError, AttributeError):
        exec_id = None
    if not exec_id:
        raise RuntimeError(f"docker exec create failed: {create.stdout or create.stderr}")

    proc = _docker_api("POST", f"/exec/{exec_id}/start",
                       {"Detach": False, "Tty": True}, stream=True)
    chunks = []
    for line in proc.stdout:
        chunks.append(line)
        _ingest_append(line)
    proc.wait()

    inspect = _docker_api("GET", f"/exec/{exec_id}/json")
    try:
        exit_code = json.loads(inspect.stdout).get("ExitCode")
    except ValueError:
        exit_code = None
    if exit_code is None:
        raise RuntimeError("could not determine ingestion exit code")
    return exit_code, "".join(chunks)


def _ingest_worker(source, env_overrides, max_passes):
    exit_code = None
    try:
        for i in range(max_passes):
            if i:
                _ingest_append(f"\n===== pass {i + 1} =====\n")
            exit_code, output = _run_ingest_pass(source, env_overrides)
            with INGEST_LOCK:
                INGEST_STATE["passes"] = i + 1
                stop_requested = INGEST_STATE["stop_requested"]
            if stop_requested:
                _ingest_append("\nStopped by user.\n")
                break
            # The pipeline container is busy (almost always the nightly cron run
            # holding the vault flock). run-ingest.sh exits 0 with this line, so
            # without special-casing it a drain would spin futile passes.
            if "another ingestion run is already in progress" in output:
                with INGEST_LOCK:
                    INGEST_STATE["blocked"] = True
                _ingest_append("\nThe pipeline is busy with another run (likely the "
                               "nightly cron job). Nothing was ingested; try again later.\n")
                break
            # run-ingest.sh exits 0 both after filing a batch and when the
            # fetcher found nothing (benign exit 20), so the drained signal is
            # its log line, not the exit code.
            if exit_code != 0 or "Nothing new to ingest" in output:
                break
            # A dry run never commits the ledger/watermark, so a second pass
            # would just re-stage the same emails - one pass is all there is.
            # (Belt to the endpoint's braces: this also catches dry-run-by-env
            # when the request didn't pass dry_run explicitly.)
            if "Dry-run complete" in output:
                if max_passes > 1:
                    _ingest_append("\nDry run: single pass only (dry runs don't advance "
                                   "the ledger/watermark, so draining would repeat the "
                                   "same batch).\n")
                break
        else:
            _ingest_append(f"\nStopped after {max_passes} passes (safety cap); "
                           "run again to continue draining.\n")
    except Exception as e:  # noqa: BLE001
        _ingest_append(f"\nERROR: {e}\n")
        exit_code = -1
    finally:
        with INGEST_LOCK:
            INGEST_STATE["running"] = False
            INGEST_STATE["exit_code"] = exit_code
            INGEST_STATE["finished"] = time.time()


def _valid_date(s):
    if not isinstance(s, str):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _start_ingest(source, env_overrides, max_passes):
    """Claim the shared run slot and spawn the worker, or 409 if one is running."""
    with INGEST_LOCK:
        if INGEST_STATE["running"]:
            busy = INGEST_STATE["source"] or "another"
            return jsonify({"error": f"a {busy} ingestion run is already in progress"}), 409
        INGEST_STATE.update(running=True, source=source, log="", exit_code=None, passes=0,
                            started=time.time(), finished=None, stop_requested=False,
                            blocked=False)
    threading.Thread(target=_ingest_worker, args=(source, env_overrides, max_passes),
                     daemon=True).start()
    return jsonify({"ok": True})


def _window_ingest_body(prefix, body):
    """Validate a watermark/window ingest request (personal email or a chat
    source) -> (env_overrides, max_passes) or an (error_response, status) tuple.
    All such sources share the same options - lookback / explicit date window /
    drain / dry-run - differing only in their env-var PREFIX."""
    lookback = body.get("lookback_days")
    if lookback is not None:
        if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 0:
            return jsonify({"error": "lookback_days must be a non-negative integer"}), 400
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        # Telegram accepts the sentinel "all" for a full-history backfill.
        if value is not None and value != "all" and not _valid_date(value):
            return jsonify({"error": f"{label} must be a YYYY-MM-DD string"}), 400
    if end_date is not None and start_date is None:
        return jsonify({"error": "end_date requires start_date"}), 400
    if (start_date is not None and end_date is not None
            and start_date != "all" and end_date != "all" and end_date < start_date):
        return jsonify({"error": "end_date must not be before start_date"}), 400
    if start_date is not None and lookback is not None:
        return jsonify({"error": "use either lookback_days or start_date/end_date, not both"}), 400
    dry_run = body.get("dry_run")
    if dry_run is not None and not isinstance(dry_run, bool):
        return jsonify({"error": "dry_run must be a boolean"}), 400

    env_overrides = []
    if lookback is not None:
        env_overrides.append(f"{prefix}_LOOKBACK_DAYS={lookback}")
    if start_date is not None:
        env_overrides.append(f"{prefix}_START_DATE={start_date}")
    if end_date is not None:
        env_overrides.append(f"{prefix}_END_DATE={end_date}")
    if dry_run is not None:
        env_overrides.append(f"{prefix}_DRY_RUN={1 if dry_run else 0}")
    max_passes = INGEST_MAX_PASSES if body.get("drain", True) else 1
    if dry_run:
        max_passes = 1  # dry runs never advance state; draining would repeat the batch
    return env_overrides, max_passes


def _ingest_personal_body(body):
    return _window_ingest_body("PERSONAL_MAIL", body)


def _ingest_telegram_body(body):
    return _window_ingest_body("TELEGRAM", body)


def _ingest_whatsapp_body(body):
    return _window_ingest_body("WHATSAPP", body)


def _ingest_digest_body(body):
    """Validate a work-digest request -> (env_overrides, max_passes) or an
    (error_response, status) tuple. The digest has no watermark/lookback/window/
    drain - only a dry-run toggle and an optional work-date backfill."""
    for bad in ("lookback_days", "start_date", "end_date", "drain"):
        if bad in body:
            return jsonify({"error": f"{bad} is not supported for the work digest"}), 400
    dry_run = body.get("dry_run")
    if dry_run is not None and not isinstance(dry_run, bool):
        return jsonify({"error": "dry_run must be a boolean"}), 400
    work_date = body.get("work_date")
    if work_date is not None and not _valid_date(work_date):
        return jsonify({"error": "work_date must be a YYYY-MM-DD string"}), 400

    env_overrides = []
    if dry_run is not None:
        # DIGEST_DRY_RUN (the prefixed var run-ingest.sh resolves ahead of the
        # global DRY_RUN) so a manual run's toggle is authoritative without
        # touching .env or the nightly cron behavior.
        env_overrides.append(f"DIGEST_DRY_RUN={1 if dry_run else 0}")
    if work_date:
        env_overrides.append(f"WORK_DATE={work_date}")
    return env_overrides, 1  # one email/day; draining is a no-op


@app.route("/api/ingest/<source>", methods=["POST"])
def ingest_source(source):
    """Manually run an ingestion source (personal | digest) in the pipeline
    container, mirroring the nightly cron job but on demand."""
    if source not in INGEST_SOURCES:
        return jsonify({"error": f"unknown source '{source}'"}), 404
    body = request.get_json(force=True, silent=True) or {}
    parser = {
        "personal": _ingest_personal_body,
        "digest": _ingest_digest_body,
        "telegram": _ingest_telegram_body,
        "whatsapp": _ingest_whatsapp_body,
    }[source]
    result = parser(body)
    # Parsers return (env_overrides:list, max_passes:int) on success, or a Flask
    # (error_response, status_code) tuple on a bad request.
    if not isinstance(result[0], list):
        return result
    env_overrides, max_passes = result
    return _start_ingest(source, env_overrides, max_passes)


@app.route("/api/ingest/<source>/stop", methods=["POST"])
def ingest_stop(source):
    """Stop the in-flight manual ingestion run: flag the worker to not start
    another pass, then SIGTERM the run's processes inside the pipeline
    container. Safe mid-batch: the ledger/watermark only commit at the end of
    a successful pass, so an aborted batch is re-fetched by the next run."""
    if source not in INGEST_SOURCES:
        return jsonify({"error": f"unknown source '{source}'"}), 404
    with INGEST_LOCK:
        if not INGEST_STATE["running"]:
            return jsonify({"error": "no ingestion run in progress"}), 409
        if INGEST_STATE["source"] != source:
            return jsonify({"error": f"a {INGEST_STATE['source']} run is in progress, not {source}"}), 409
        INGEST_STATE["stop_requested"] = True
    create = _docker_api("POST", f"/containers/{PIPELINE_CONTAINER}/exec", {
        "AttachStdout": True, "AttachStderr": True, "Tty": True,
        # pkill exits 1 when nothing matched (e.g. stop pressed between passes);
        # that's fine - the worker also checks stop_requested between passes.
        # Covers both fetchers and both wrappers so either source can be stopped.
        "Cmd": ["pkill", "-TERM", "-f",
                "run-ingest.sh|run-digest.sh|fetch_inbox.py|fetch_digest.py|"
                "fetch_telegram.py|fetch_whatsapp.py|custom_agent_loop.py"],
    })
    try:
        exec_id = json.loads(create.stdout).get("Id")
    except (ValueError, AttributeError):
        exec_id = None
    if not exec_id:
        return jsonify({"error": f"docker exec create failed: {create.stdout or create.stderr}"}), 500
    _docker_api("POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": True})
    return jsonify({"ok": True})


@app.route("/api/ingest/status")
@app.route("/api/ingest/<source>/status")  # back-compat alias; state is global
def ingest_status(source=None):
    with INGEST_LOCK:
        return jsonify(dict(INGEST_STATE))


# --- Telegram login (dashboard-driven Telethon sign-in) --------------------
# vault-qa reaches the repo at /host, so the session file it writes here
# (/host/data/telegram/telegram.session) is the same host file the pipeline
# container reads at /work/telegram/telegram.session. All Telethon calls run on
# one dedicated worker thread so the client's asyncio loop keeps a single owner
# across the multi-step (phone -> code -> password) flow.
import concurrent.futures  # noqa: E402

_TG_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg-login")
TELEGRAM_LOGIN_LOCK = threading.Lock()
TELEGRAM_LOGIN = {"phone": None, "phone_code_hash": None, "client": None}


def _tg_run(fn):
    return _TG_EXECUTOR.submit(fn).result()


def _telegram_paths():
    base = os.path.join(HOST_DIR, "data", "telegram")
    return os.path.join(base, "telegram.session"), os.path.join(base, "chats.json"), base


def _telegram_creds():
    try:
        api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
    except ValueError:
        api_id = 0
    return api_id, os.environ.get("TELEGRAM_API_HASH", "")


def _telegram_client():
    from telethon.sync import TelegramClient
    api_id, api_hash = _telegram_creds()
    session, _, base = _telegram_paths()
    os.makedirs(base, exist_ok=True)
    return TelegramClient(session, api_id, api_hash)


def _me_name(me):
    return " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")])) \
        or getattr(me, "username", None) or str(getattr(me, "id", ""))


def _tg_dump_chats(client):
    import fetch_telegram as ft
    _, chats_json, base = _telegram_paths()
    os.makedirs(base, exist_ok=True)
    rows = []
    for d in client.iter_dialogs():
        rows.append({"id": d.id, "title": d.name or "",
                     "username": getattr(d.entity, "username", None),
                     "type": ft.dialog_type(d)})
    with open(chats_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


@app.route("/api/telegram/status")
def telegram_status():
    api_id, api_hash = _telegram_creds()
    if not api_id or not api_hash:
        return jsonify({"configured": False, "authorized": False,
                        "error": "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in Settings first."})

    def op():
        with TELEGRAM_LOGIN_LOCK:
            login_active = TELEGRAM_LOGIN.get("client") is not None
        if login_active:
            # A sign-in is mid-flight and its client holds the SQLite session
            # file open; a second client on the same file raises "database is
            # locked", so report without touching the session.
            return {"configured": True, "authorized": False, "login_in_progress": True}
        client = _telegram_client()
        client.connect()
        try:
            authorized = client.is_user_authorized()
            info = {"configured": True, "authorized": authorized}
            if authorized:
                info["name"] = _me_name(client.get_me())
            return info
        finally:
            client.disconnect()
    try:
        return jsonify(_tg_run(op))
    except Exception as e:  # noqa: BLE001
        return jsonify({"configured": True, "authorized": False, "error": str(e)})


@app.route("/api/telegram/login/start", methods=["POST"])
def telegram_login_start():
    body = request.get_json(force=True, silent=True) or {}
    phone = (body.get("phone") or "").strip()
    if not phone:
        return jsonify({"error": "phone is required (international format, e.g. +6591234567)"}), 400

    def op():
        # Tear down any client left over from a previous attempt BEFORE opening
        # a new one: two Telethon clients on the same SQLite session file is
        # what raises "database is locked".
        with TELEGRAM_LOGIN_LOCK:
            old = TELEGRAM_LOGIN.get("client")
            TELEGRAM_LOGIN.update(phone=None, phone_code_hash=None, client=None)
        if old is not None:
            try:
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        client = _telegram_client()
        try:
            client.connect()
            if client.is_user_authorized():
                client.disconnect()
                return {"already_authorized": True}
            sent = client.send_code_request(phone)
        except BaseException:
            # A leaked connected client would hold the session DB open and
            # wedge every later attempt with "database is locked".
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise
        with TELEGRAM_LOGIN_LOCK:
            TELEGRAM_LOGIN.update(phone=phone, phone_code_hash=sent.phone_code_hash, client=client)
        return {"ok": True, "code_sent": True}
    try:
        return jsonify(_tg_run(op))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400


@app.route("/api/telegram/login/code", methods=["POST"])
def telegram_login_code():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    with TELEGRAM_LOGIN_LOCK:
        phone = TELEGRAM_LOGIN.get("phone")
        pch = TELEGRAM_LOGIN.get("phone_code_hash")
        client = TELEGRAM_LOGIN.get("client")
    if not phone or not pch or client is None:
        return jsonify({"error": "no login in progress; enter your phone number first"}), 400
    if not code:
        return jsonify({"error": "code is required"}), 400

    def op():
        from telethon.errors import SessionPasswordNeededError
        try:
            client.sign_in(phone=phone, code=code, phone_code_hash=pch)
        except SessionPasswordNeededError:
            return {"needs_password": True}
        # Signed in: release the client even if the post-login extras fail, or
        # it would hold the session DB open ("database is locked" elsewhere).
        try:
            name = _me_name(client.get_me())
            _tg_dump_chats(client)
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            with TELEGRAM_LOGIN_LOCK:
                TELEGRAM_LOGIN.update(phone=None, phone_code_hash=None, client=None)
        return {"ok": True, "authorized": True, "name": name}
    try:
        return jsonify(_tg_run(op))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400


@app.route("/api/telegram/login/password", methods=["POST"])
def telegram_login_password():
    body = request.get_json(force=True, silent=True) or {}
    password = body.get("password") or ""
    with TELEGRAM_LOGIN_LOCK:
        client = TELEGRAM_LOGIN.get("client")
    if client is None:
        return jsonify({"error": "no login in progress; enter your phone number first"}), 400
    if not password:
        return jsonify({"error": "password is required"}), 400

    def op():
        client.sign_in(password=password)
        # Same as the code step: never keep the client past a successful login.
        try:
            name = _me_name(client.get_me())
            _tg_dump_chats(client)
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            with TELEGRAM_LOGIN_LOCK:
                TELEGRAM_LOGIN.update(phone=None, phone_code_hash=None, client=None)
        return {"ok": True, "authorized": True, "name": name}
    try:
        return jsonify(_tg_run(op))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400


@app.route("/api/telegram/chats")
def telegram_chats():
    _, chats_json, _ = _telegram_paths()
    if not os.path.exists(chats_json):
        return jsonify({"chats": [], "note": "No chats discovered yet - log in, then run a "
                        "Telegram ingest (or the login itself refreshes this list)."})
    try:
        with open(chats_json, "r", encoding="utf-8") as f:
            return jsonify({"chats": json.load(f)})
    except (OSError, ValueError) as e:
        return jsonify({"chats": [], "error": str(e)})


# --- WhatsApp bridge status/QR/chats (the bridge itself is the Node service;
# these routes just surface the files it writes under /host/data/whatsapp) ----
WHATSAPP_BRIDGE_CONTAINER = "copilot-digest-whatsapp-bridge"


def _whatsapp_dir():
    return os.path.join(HOST_DIR, "data", "whatsapp")


@app.route("/api/whatsapp/status")
def whatsapp_status():
    path = os.path.join(_whatsapp_dir(), "status.json")
    if not os.path.exists(path):
        return jsonify({"state": "unknown",
                        "error": "bridge not started yet (docker compose up -d whatsapp-bridge)"})
    try:
        with open(path, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (OSError, ValueError) as e:
        return jsonify({"state": "unknown", "error": str(e)})


@app.route("/api/whatsapp/qr")
def whatsapp_qr():
    path = os.path.join(_whatsapp_dir(), "qr.png")
    if not os.path.exists(path):
        return jsonify({"error": "no QR available (already paired, or bridge not waiting)"}), 404
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "no-store, must-revalidate"})


@app.route("/api/whatsapp/chats")
def whatsapp_chats():
    path = os.path.join(_whatsapp_dir(), "chats.json")
    if not os.path.exists(path):
        return jsonify({"chats": [], "note": "No chats discovered yet - pair WhatsApp and let "
                        "the history sync run."})
    try:
        with open(path, "r", encoding="utf-8") as f:
            return jsonify({"chats": json.load(f)})
    except (OSError, ValueError) as e:
        return jsonify({"chats": [], "error": str(e)})


@app.route("/api/whatsapp/repair", methods=["POST"])
def whatsapp_repair():
    """Log out the current WhatsApp link and force a fresh QR: move the auth dir
    aside and restart the bridge, which then has no creds and emits a new QR."""
    auth_dir = os.path.join(_whatsapp_dir(), "auth")
    if os.path.isdir(auth_dir):
        stamp = now_ts_label()
        try:
            os.rename(auth_dir, auth_dir + f".bak-{stamp}")
        except OSError as e:
            return jsonify({"error": f"could not move auth dir: {e}"}), 500
    # Clear stale QR/status so the UI doesn't briefly show the old state.
    for name in ("qr.png", "status.json"):
        p = os.path.join(_whatsapp_dir(), name)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    _docker_api("POST", f"/containers/{WHATSAPP_BRIDGE_CONTAINER}/restart")
    return jsonify({"ok": True})


def now_ts_label():
    from tzutil import now_local
    return now_local().strftime("%Y%m%d-%H%M%S")


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
                payload = dict(finish_args)
                _verify_citations(payload.get("citations"))
                emit_answer(payload)
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


@app.route("/api/simulator/email", methods=["POST"])
def simulator_email():
    body = request.get_json(force=True, silent=True) or {}
    sim_type = body.get("type", "personal")
    content = body.get("content", "")
    
    if sim_type == "digest":
        prompt_file = "/app/prompt_dry_run.txt"
        target_file = "digest.md"
    else:
        prompt_file = "/app/prompt_personal_email_dry_run.txt"
        target_file = "personal.md"
        
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Write the content
        with open(os.path.join(tmpdir, target_file), "w", encoding="utf-8") as f:
            f.write(content)
            
        # 2. Write vault_index.txt
        vault_index = os.path.join(tmpdir, "vault_index.txt")
        try:
            index_lines = []
            for root, dirs, files in os.walk(cal.VAULT_DIR):
                dirs[:] = [d for d in dirs if not d.startswith(".")
                           and d not in ("Attachments", "smart-chats")]
                for fname in files:
                    if fname.endswith(".md") and not fname.startswith("."):
                        abs_path = os.path.join(root, fname)
                        rel = os.path.relpath(abs_path, cal.VAULT_DIR)
                        try:
                            mtime = os.path.getmtime(abs_path)
                        except OSError:
                            mtime = 0
                        # Same "YYYY-MM-DD<TAB>path" format run-ingest.sh writes.
                        index_lines.append((rel, f"{datetime.fromtimestamp(mtime, APP_TZ):%Y-%m-%d}\t{rel}"))
            with open(vault_index, "w", encoding="utf-8") as f:
                f.write("\n".join(line for _, line in sorted(index_lines)))
        except Exception as e:
            print(f"Simulator warning: failed to generate vault_index.txt: {e}", file=sys.stderr)
            
        # 3. Setup ENV
        env = os.environ.copy()
        env["STAGING_DIR"] = tmpdir
        env["DRY_RUN"] = "1"
        
        # 4. Patch the prompt to point to tmpdir instead of hardcoded paths
        with open(prompt_file, "r", encoding="utf-8") as f:
            prompt_text = f.read()
            
        if sim_type == "digest":
            prompt_text = prompt_text.replace("/work/staging", tmpdir)
        else:
            prompt_text = prompt_text.replace("/work/staging/personal", tmpdir)
            
        sim_prompt_file = os.path.join(tmpdir, "prompt.txt")
        with open(sim_prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt_text)
        
        # 5. Run agent
        cmd = [sys.executable, "/app/scripts/custom_agent_loop.py", sim_prompt_file]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        
        # 6. Read proposed.md
        proposed_path = os.path.join(tmpdir, "proposed.md")
        proposed_content = ""
        if os.path.exists(proposed_path):
            with open(proposed_path, "r", encoding="utf-8") as f:
                proposed_content = f.read()
                
        return jsonify({
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "proposed": proposed_content,
            "exit_code": proc.returncode
        })


def _init_client():
    global CLIENT, MODEL
    try:
        # Chat can use a stronger model than nightly filing via VAULT_QA_LLM_*
        # (falls back to the global LLM_* vars when unset).
        CLIENT, MODEL = cal.make_client(prefix="VAULT_QA_")
    except cal.ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


_init_client()
store.init()


if __name__ == "__main__":
    from waitress import serve
    print(f"Starting vault chat UI on :{PORT} (model {MODEL})...", file=sys.stderr)
    serve(app, host="0.0.0.0", port=PORT, threads=4)
