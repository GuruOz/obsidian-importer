#!/usr/bin/env python3
"""Custom tool-calling agent loop, powered by OpenAI-compatible APIs (OpenRouter, DeepSeek, etc)."""
import fnmatch
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from openai import OpenAI

# Environment setup
VAULT_DIR = os.path.normpath(os.environ.get("VAULT_DIR", "/vault"))
STAGING_DIR = os.path.normpath(os.environ.get("STAGING_DIR", "/work/staging"))
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"


class ConfigError(RuntimeError):
    """Raised by make_client() when LLM_* env vars are missing/invalid."""


class AgentAPIError(RuntimeError):
    """Raised by run_loop() when the LLM endpoint call fails."""


class AgentCancelled(RuntimeError):
    """Raised by run_loop() when the caller's cancel_event fires mid-run.
    Carries whatever answer text had already streamed (possibly empty) so the
    caller can solidify the partial response."""
    def __init__(self, partial_answer=""):
        super().__init__("cancelled by caller")
        self.partial_answer = partial_answer


def _vlog(msg):
    """Timestamped diagnostic line on stderr. This is the execution log the
    simulator tab, the settings-page ingestion log, and the per-run
    agent.<source>.<date>.json files all surface, so every step of a run is
    reconstructable after the fact."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def _one_line(text, max_len=300):
    """Collapse a tool result / model message to one truncated log line."""
    s = " ".join(str(text).split())
    return s[:max_len] + f"...[{len(s)} chars total]" if len(s) > max_len else s


def _summarize_args(args, max_len=200):
    """Render tool args for logging. Long string values (e.g. write_file's
    `content`) are truncated so a large payload doesn't flood the log - just
    enough to see what path/pattern/query the tool was actually called with."""
    if not isinstance(args, dict):
        return repr(args)
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_len:
            v = v[:max_len] + f"...[{len(v)} chars total]"
        parts.append(f"{k}={v!r}")
    return "{" + ", ".join(parts) + "}"


def _looks_like_error(result):
    return isinstance(result, str) and (result.startswith("Error") or result.startswith("Unknown function"))


def safe_path(p: str) -> str:
    """Ensure path is within the allowed directories (VAULT_DIR or STAGING_DIR)."""
    if not os.path.isabs(p):
        p = os.path.join(VAULT_DIR, p)
    p = os.path.normpath(p)
    # Bare startswith would also match sibling dirs (e.g. "/vault2" vs "/vault"),
    # so require an exact root match or a path-separator boundary.
    if not any(p == root or p.startswith(root + os.sep) for root in (VAULT_DIR, STAGING_DIR)):
        raise ValueError(f"Access denied: {p}")
    return p

# --- Tools Implementation ---

# Directories that hold no filing/answer targets: dotdirs (.obsidian/.trash/
# .smart-env), OneNote-import attachments, and raw exported chat logs. Skipped by
# the nightly vault index and search_relevant; grep_search honors the same list so
# it never surfaces plugin/config markdown the rest of the pipeline hides.
_EXCLUDED_DIRS = ("Attachments", "smart-chats")

# read_file default ceiling. Generous on purpose: the staged digest.md (9+
# workstreams) is this tool's biggest legitimate payload and must not truncate
# silently mid-ingestion. Chat questions rarely need a whole huge note at once
# and can page with offset/limit.
READ_FILE_MAX_CHARS = 50_000
# grep_search caps: at most this many matching lines per file, and this many
# total, so one noisy file can't crowd out the rest and a broad pattern reports
# how much it hid instead of silently dropping it.
GREP_MAX_PER_FILE = 5
GREP_MAX_TOTAL = 100


def _pruned_walk(root_dir):
    """os.walk over the vault with the excluded dirs pruned in-place (dot-dirs +
    _EXCLUDED_DIRS), yielding (root, files) - the shared traversal for grep."""
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _EXCLUDED_DIRS]
        yield root, files


def read_file(path: str, offset: int = 0, limit: int = None) -> str:
    """Read a file. Reads at most READ_FILE_MAX_CHARS characters (or `limit` if
    smaller) starting at character `offset`; when content is cut off, appends an
    explicit notice with the byte range and how to continue, so the model never
    silently acts on a truncated note."""
    try:
        p = safe_path(path)
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading file: {e}"
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        cap = int(limit) if limit else READ_FILE_MAX_CHARS
    except (TypeError, ValueError):
        cap = READ_FILE_MAX_CHARS
    cap = max(1, min(cap, READ_FILE_MAX_CHARS))
    total = len(content)
    window = content[offset:offset + cap]
    end = offset + len(window)
    if offset > total:
        return (f"[offset {offset} is past end of file ({total} chars). "
                f"Call read_file with a smaller offset.]")
    if offset > 0 or end < total:
        return (window + f"\n\n[showing chars {offset}-{end} of {total}."
                + (f" Call read_file with offset={end} to continue.]" if end < total else "]"))
    return window

def glob_search(pattern: str) -> str:
    """Find files in the vault matching a glob pattern."""
    matches = []
    for root, files in _pruned_walk(VAULT_DIR):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), VAULT_DIR)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f, pattern):
                matches.append(rel)
    return "\n".join(matches) if matches else "No files found."

def grep_search(query: str) -> str:
    """Search for a regex inside every markdown note (excluded dirs pruned).
    Invalid regex falls back to a literal search rather than erroring. Results
    are grouped per file and files ranked by match count, capped per-file and
    overall, with an explicit note of anything truncated."""
    literal = False
    try:
        regex = re.compile(query, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(query), re.IGNORECASE)
        literal = True

    per_file = {}          # rel path -> [ "line: text", ... ] (capped)
    hidden_in_file = {}    # rel path -> count of matches beyond the per-file cap
    try:
        for root, files in _pruned_walk(VAULT_DIR):
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, VAULT_DIR)
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as fobj:
                        for i, line in enumerate(fobj):
                            if regex.search(line):
                                lst = per_file.setdefault(rel, [])
                                if len(lst) < GREP_MAX_PER_FILE:
                                    lst.append(f"{i+1}: {line.strip()}")
                                else:
                                    hidden_in_file[rel] = hidden_in_file.get(rel, 0) + 1
                except Exception:
                    pass
    except Exception as e:
        return f"Error in grep: {e}"

    if not per_file:
        return "No matches found."

    # Rank files by total hits (shown + hidden), most-relevant first.
    def total_hits(rel):
        return len(per_file[rel]) + hidden_in_file.get(rel, 0)
    ranked = sorted(per_file, key=lambda r: -total_hits(r))

    out, shown, files_omitted, matches_omitted = [], 0, 0, 0
    prefix = "Note: query was not valid regex; searched for it literally.\n" if literal else ""
    for rel in ranked:
        if shown >= GREP_MAX_TOTAL:
            files_omitted += 1
            matches_omitted += total_hits(rel)
            continue
        for entry in per_file[rel]:
            if shown >= GREP_MAX_TOTAL:
                matches_omitted += 1
                continue
            out.append(f"{rel}:{entry}")
            shown += 1
        if hidden_in_file.get(rel):
            out.append(f"{rel}: ... +{hidden_in_file[rel]} more match(es) in this file")
    if files_omitted or matches_omitted:
        out.append(f"... truncated: {matches_omitted} more match(es) in {files_omitted} "
                   "more file(s). Narrow the pattern to see them.")
    return prefix + "\n".join(out)

def write_file(path: str, content: str) -> str:
    """Write or overwrite a file."""
    # Filing_Rules.md is exempt so the run-once profiler works before go-live (M5).
    if DRY_RUN and not path.endswith(("proposed.md", "Filing_Rules.md")):
        return f"Error: DRY_RUN is enabled. You can only write to proposed.md in staging."
    try:
        p = safe_path(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"

def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace exactly one occurrence of old_string with new_string in a file."""
    if DRY_RUN:
        return "Error: DRY_RUN is enabled. You cannot edit vault files."
    try:
        p = safe_path(path)
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
        
        if content.count(old_string) == 0:
            return "Error: old_string not found in file."
        elif content.count(old_string) > 1:
            return "Error: old_string occurs multiple times. Please provide a more specific old_string."
            
        new_content = content.replace(old_string, new_string)
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"Successfully edited {path}"
    except Exception as e:
        return f"Error editing file: {e}"


# --- Tool Schemas ---
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": ("Read the contents of a file. Returns up to 50000 characters; if the "
                            "file is longer the result ends with a notice giving the character range "
                            "and the offset to continue from."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to vault)"},
                    "offset": {"type": "integer", "description": "Character offset to start reading from (default 0)."},
                    "limit": {"type": "integer", "description": "Max characters to return (default/again capped at 50000)."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files in the vault matching a glob pattern (e.g. '*.md' or 'Work/*').",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": ("Search for a regex inside every markdown note. Results are grouped by "
                            "file and ranked by match count. An invalid regex is searched literally "
                            "instead of erroring. Use for exact identifiers (ticket numbers, names)."),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write new content to a file (overwrites if it exists).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exactly one occurrence of old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "The exact string to replace. Must match exactly including whitespace."},
                    "new_string": {"type": "string", "description": "The string to replace it with."}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this when you have successfully completed the task to output the final status JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "work_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "files_touched": {"type": "array", "items": {"type": "string"}},
                    "entries_filed": {"type": "integer"},
                    "sections": {"type": "integer"},
                    "skipped": {"type": "integer", "description": "Count of source items triaged out and deliberately not filed."},
                    "skipped_details": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One short line per skipped item, e.g. 'Newsletter: ACME weekly — marketing'."
                    }
                },
                "required": ["status"]
            }
        }
    }
]

def render_prompt(prompt):
    """Substitute the {{DATE_CONTEXT}} placeholder, if present.

    This is the single place dates enter the prompt: today's date + weekday are
    always injected (the model has no reliable idea what 'today' is otherwise),
    and an optional WORK_DATE env var (YYYY-MM-DD) forces the filing date instead
    of letting the agent infer it from the digest - for backfilling a specific day.
    Prompts without the placeholder (profiler, weekly rollup) pass through
    untouched, and WORK_DATE is ignored for them.
    """
    if "{{DATE_CONTEXT}}" not in prompt:
        return prompt
    now = datetime.now()
    ctx = f"DATE CONTEXT: today is {now:%Y-%m-%d} ({now:%A})."
    work_date = os.environ.get("WORK_DATE", "").strip()
    if work_date:
        try:
            wd = datetime.strptime(work_date, "%Y-%m-%d")
        except ValueError:
            raise ConfigError(f"WORK_DATE must be YYYY-MM-DD, got {work_date!r}")
        ctx += (f" The WORK DATE for this digest is {wd:%Y-%m-%d} ({wd:%A}) - file under "
                "this date and use it in the idempotency marker; do not infer a "
                "different date from the digest.")
    return prompt.replace("{{DATE_CONTEXT}}", ctx)


def make_client(prefix=""):
    """Build the OpenAI-compatible client from {prefix}LLM_* env vars, or raise
    ConfigError. `prefix` lets a caller use its own endpoint while falling back
    to the global one: e.g. make_client("VAULT_QA_") reads VAULT_QA_LLM_BASE_URL/
    _MODEL/_API_KEY and only uses the plain LLM_* vars where those are unset - so
    the chat can point at a stronger model than the nightly filing agent.

    LLM_API_KEY is optional so local OpenAI-compatible servers work out of the
    box - Ollama (http://host.docker.internal:11434/v1), LM Studio, llama.cpp
    etc. accept any key. The SDK insists on a non-empty string, so a harmless
    placeholder is sent when none is configured; hosted providers will simply
    reject it, which surfaces as a clear 401 instead of a missing-var error.
    """
    def _get(suffix):
        return os.environ.get(f"{prefix}{suffix}") or os.environ.get(suffix)
    api_key = _get("LLM_API_KEY") or "ollama"
    base_url = _get("LLM_BASE_URL")
    model = _get("LLM_MODEL")
    if not all([base_url, model]):
        raise ConfigError("LLM_BASE_URL and LLM_MODEL must be set for custom agent "
                          "(plus LLM_API_KEY unless the endpoint is a local server like Ollama).")
    return OpenAI(api_key=api_key, base_url=base_url), model


class _AnswerExtractor:
    """Incrementally extracts the string value of the top-level "answer" key
    from the finish tool call's JSON arguments as they stream in, emitting
    decoded text fragments via emit(). Best-effort: if the model orders the
    keys differently or the JSON is odd, nothing is emitted and the caller
    falls back to the fully parsed args at the end of the stream."""

    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t"}

    def __init__(self, emit=None):
        self.emit = emit
        self.text = ""
        self._buf = ""
        self._state = "seek"

    def feed(self, fragment):
        self._buf += fragment
        if self._state == "seek":
            m = re.search(r'"answer"\s*:\s*"', self._buf)
            if m is None:
                self._buf = self._buf[-16:]  # keep enough tail to match a key split across chunks
                return
            self._buf = self._buf[m.end():]
            self._state = "instring"
        if self._state != "instring":
            return
        out = []
        i, n = 0, len(self._buf)
        while i < n:
            c = self._buf[i]
            if c == '"':
                self._state = "done"
                i += 1
                break
            if c == "\\":
                if i + 1 >= n:
                    break  # escape split across chunks; resume on next feed
                e = self._buf[i + 1]
                if e == "u":
                    if i + 6 > n:
                        break
                    try:
                        out.append(chr(int(self._buf[i + 2:i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                else:
                    out.append(self._ESCAPES.get(e, e))
                    i += 2
            else:
                out.append(c)
                i += 1
        self._buf = self._buf[i:]
        if out:
            piece = "".join(out)
            self.text += piece
            if self.emit is not None:
                try:
                    self.emit(piece)
                except Exception:
                    pass  # streaming must never break the agent loop


def _add_usage(usage_out, usage):
    if usage_out is not None and usage is not None:
        usage_out["input_tokens"] = usage_out.get("input_tokens", 0) + (getattr(usage, "prompt_tokens", 0) or 0)
        usage_out["output_tokens"] = usage_out.get("output_tokens", 0) + (getattr(usage, "completion_tokens", 0) or 0)


def _stream_completion(client, model, messages, tools, cancel_event, extractor, usage_out):
    """One streamed chat.completions call. Returns (assistant_msg_dict, calls,
    cancelled) where calls is [(id, name, arguments_json_str)] in index order.
    finish-call argument deltas are fed to `extractor` as they arrive. On
    cancellation the partial tool calls are discarded (calls comes back empty)."""
    kwargs = dict(model=model, messages=messages, tools=tools,
                  tool_choice="auto", stream=True)
    try:
        try:
            stream = client.chat.completions.create(
                stream_options={"include_usage": True}, **kwargs)
        except Exception as e:
            if "stream_options" not in str(e):
                raise
            stream = client.chat.completions.create(**kwargs)  # provider doesn't support it
    except Exception as e:
        raise AgentAPIError(f"API Error: {e}") from e

    content_parts, by_index, cancelled = [], {}, False
    try:
        for chunk in stream:
            _add_usage(usage_out, getattr(chunk, "usage", None))
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
            for tc in (getattr(delta, "tool_calls", None) or []):
                entry = by_index.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    entry["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        entry["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        entry["arguments"] += fn.arguments
                        if entry["name"] == "finish":
                            extractor.feed(fn.arguments)
    except Exception as e:
        raise AgentAPIError(f"API Error mid-stream: {e}") from e
    finally:
        try:
            stream.close()
        except Exception:
            pass

    if cancelled:
        return {"role": "assistant", "content": "".join(content_parts) or None}, [], True
    calls = [(v["id"], v["name"], v["arguments"]) for _, v in sorted(by_index.items())]
    msg = {"role": "assistant", "content": "".join(content_parts) or None}
    if calls:
        msg["tool_calls"] = [{"id": cid, "type": "function",
                              "function": {"name": name, "arguments": args}}
                             for cid, name, args in calls]
    return msg, calls, False


def run_loop(client, model, messages, tools, handlers, max_loops=30, progress_cb=None,
             usage_out=None, stream_cb=None, cancel_event=None):
    """Drive the tool-calling loop until the model calls `finish`.

    `handlers` maps tool name -> callable(args_dict) -> str. Returns the finish
    call's arguments dict, or None if max_loops was exhausted. Raises AgentAPIError
    on an LLM endpoint failure. `progress_cb(tool_name, args_dict)`, if given, is
    called right before each tool is dispatched (never allowed to break the loop).
    `usage_out`, if given a dict, accumulates input_tokens/output_tokens across
    every API call in the loop.

    `stream_cb(text)` and/or `cancel_event` (a threading.Event) switch the API
    calls to streaming mode: stream_cb receives the finish answer's text
    incrementally as it is generated, and a set cancel_event aborts the run at
    the next chunk/tool boundary by raising AgentCancelled (carrying any partial
    answer text). The message history is left valid for a follow-up turn either
    way. Callers that pass neither get the original non-streaming behavior.
    """
    use_stream = stream_cb is not None or cancel_event is not None
    extractor = _AnswerExtractor(stream_cb)
    # Track per-call token deltas even when the caller shares usage_out.
    usage = usage_out if usage_out is not None else {}

    for turn in range(1, max_loops + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise AgentCancelled(extractor.text)

        _vlog(f"turn {turn}/{max_loops}: calling {model} "
              f"({len(messages)} messages in context)")
        tokens_before = (usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        t_llm = time.time()

        if use_stream:
            msg, calls, cancelled = _stream_completion(
                client, model, messages, tools, cancel_event, extractor, usage)
            if cancelled:
                # Discard the aborted completion's partial tool calls; keep the
                # partial answer as plain assistant text so the history stays valid.
                if extractor.text:
                    messages.append({"role": "assistant", "content": extractor.text})
                raise AgentCancelled(extractor.text)
            messages.append(msg)
            content = msg.get("content")
        else:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto"
                )
            except Exception as e:
                raise AgentAPIError(f"API Error: {e}") from e
            _add_usage(usage, getattr(response, "usage", None))
            msg = response.choices[0].message
            messages.append(msg)
            calls = [(tc.id, tc.function.name, tc.function.arguments)
                     for tc in (msg.tool_calls or [])]
            content = msg.content

        d_in = usage.get("input_tokens", 0) - tokens_before[0]
        d_out = usage.get("output_tokens", 0) - tokens_before[1]
        _vlog(f"turn {turn}: LLM responded in {time.time() - t_llm:.1f}s "
              f"({d_in} in / {d_out} out tokens, {len(calls)} tool call(s))")
        if content:
            # The model's inter-tool commentary was previously invisible; it is
            # often the only clue to WHY it chose a filing target.
            _vlog(f"turn {turn}: model says: {_one_line(content)}")

        if not calls:
            # Model responded with text instead of finishing. Just prompt it to finish.
            _vlog(f"turn {turn}: no tool call in response; nudging model to finish")
            messages.append({"role": "user", "content": "Please continue and use the finish tool when done."})
            continue

        for pos, (call_id, func_name, raw_args) in enumerate(calls):
            try:
                args = json.loads(raw_args)
            except Exception as e:
                error_msg = f"Error parsing JSON arguments: {e}"
                _vlog(f">> Tool Call: {func_name} - {error_msg} (raw: {_one_line(raw_args)})")
                messages.append({"role": "tool", "tool_call_id": call_id, "name": func_name,
                                 "content": error_msg})
                continue

            _vlog(f">> Tool Call: {func_name}({_summarize_args(args)})")
            if progress_cb is not None:
                try:
                    progress_cb(func_name, args)
                except Exception:
                    pass  # progress reporting must never break the agent loop

            if func_name == "finish":
                _vlog(f"finish called: {_summarize_args(args, max_len=500)}")
                # Answer this call (and any remaining parallel calls in the same
                # assistant message) before returning, so the transcript stays
                # valid if the caller reuses `messages` for a follow-up turn -
                # OpenAI-compatible APIs reject histories with unanswered tool calls.
                for cid, cname, _ in calls[pos:]:
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "name": cname, "content": "Finished."})
                return args

            if cancel_event is not None and cancel_event.is_set():
                # Answer this and every remaining call so the history stays valid.
                for cid, cname, _ in calls[pos:]:
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "name": cname, "content": "Request cancelled by user."})
                raise AgentCancelled(extractor.text)

            handler = handlers.get(func_name)
            t_tool = time.time()
            if handler is None:
                result = f"Unknown function {func_name}"
            else:
                try:
                    result = handler(args)
                except Exception as e:
                    result = f"Error in {func_name}: {e}"

            if _looks_like_error(result):
                # This is the log line that was missing: previously a tool
                # returning "Access denied"/"Error ..." was invisible unless the
                # model happened to mention it - the model can also just ignore
                # the error string and call finish() as if nothing went wrong.
                _vlog(f"   !! {func_name} returned an error: {result}")
            else:
                _vlog(f"   << {func_name} ok in {time.time() - t_tool:.1f}s "
                      f"({len(str(result))} chars): {_one_line(result, 200)}")

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": func_name,
                "content": str(result)
            })

    _vlog(f"gave up: model never called finish within {max_loops} turns")
    return None


def main():
    try:
        client, model = make_client()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    prompt_file = sys.argv[1]

    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt = f.read()
    try:
        prompt = render_prompt(prompt)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    system_content = "You are an autonomous file-management agent for an Obsidian vault."
    # Inject the vault's conventions manifest (generated by profile_vault.py) so every
    # run is guaranteed to see it, rather than relying on the model choosing to read it.
    filing_rules_path = os.path.join(VAULT_DIR, "Filing_Rules.md")
    filing_rules_chars = 0
    if os.path.exists(filing_rules_path):
        with open(filing_rules_path, "r", encoding="utf-8") as f:
            rules = f.read()
        filing_rules_chars = len(rules)
        system_content += "\n\nVault conventions (Filing_Rules.md):\n\n" + rules

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt}
    ]

    _vlog(f"agent run starting: model={model} base_url={os.environ.get('LLM_BASE_URL', '')} "
          f"DRY_RUN={DRY_RUN}")
    _vlog(f"config: VAULT_DIR={VAULT_DIR} STAGING_DIR={STAGING_DIR} "
          f"prompt={prompt_file} ({len(prompt)} chars) "
          f"Filing_Rules.md={'injected, ' + str(filing_rules_chars) + ' chars' if filing_rules_chars else 'not found'}")

    handlers = {
        "read_file": lambda a: read_file(a.get("path", ""), a.get("offset", 0), a.get("limit")),
        "glob_search": lambda a: glob_search(a.get("pattern", "")),
        "grep_search": lambda a: grep_search(a.get("query", "")),
        "write_file": lambda a: write_file(a.get("path", ""), a.get("content", "")),
        "edit_file": lambda a: edit_file(a.get("path", ""), a.get("old_string", ""), a.get("new_string", "")),
    }

    # Give the filing agent the same BM25 relevance search the chat uses, so it can
    # find the canonical topic note for a recurring workstream even when the title
    # doesn't match the digest's wording - not just shortlist from the index. Built
    # once here (a few seconds for ~1.5k notes); if it fails the run continues
    # without it rather than aborting the nightly job.
    tools = TOOLS
    try:
        import lexical_index
        _lex = lexical_index.build_index(VAULT_DIR)
        handlers["search_relevant"] = lambda a: _lex.search(
            a.get("query", ""), top_k=a.get("top_k") or 8,
            path_prefix=a.get("path_prefix"),
            date_from=a.get("date_from"), date_to=a.get("date_to"))
        tools = TOOLS + [lexical_index.SEARCH_RELEVANT_TOOL]
        _vlog(f"search_relevant enabled ({len(_lex.chunks)} chunks indexed)")
    except Exception as e:  # noqa: BLE001
        _vlog(f"search_relevant unavailable, continuing without it: {e}")

    # Long multi-workstream digests can exceed the default 30-turn budget; make it
    # tunable per source via DIGEST_MAX_LOOPS / PERSONAL_MAIL_MAX_LOOPS (resolved to
    # AGENT_MAX_LOOPS by run-ingest.sh).
    try:
        max_loops = int(os.environ.get("AGENT_MAX_LOOPS", "30") or 30)
    except ValueError:
        max_loops = 30

    usage = {}
    t_run = time.time()
    try:
        finish_args = run_loop(client, model, messages, tools, handlers,
                               max_loops=max_loops, usage_out=usage)
    except AgentAPIError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    _vlog(f"agent run finished in {time.time() - t_run:.1f}s, total tokens: "
          f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out")
    if finish_args is None:
        print(json.dumps({"error": "Exceeded maximum tool call loops"}))
        sys.exit(1)
    print(json.dumps(finish_args))

if __name__ == "__main__":
    main()
