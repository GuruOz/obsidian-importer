#!/usr/bin/env python3
"""Custom tool-calling agent loop, powered by OpenAI-compatible APIs (OpenRouter, DeepSeek, etc)."""
import fnmatch
import json
import os
import re
import sys
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

def read_file(path: str) -> str:
    """Read the contents of a file."""
    try:
        p = safe_path(path)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def glob_search(pattern: str) -> str:
    """Find files in the vault matching a glob pattern."""
    matches = []
    for root, _, files in os.walk(VAULT_DIR):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), VAULT_DIR)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f, pattern):
                matches.append(rel)
    return "\n".join(matches) if matches else "No files found."

def grep_search(query: str) -> str:
    """Search for a regex string inside all markdown files in the vault."""
    matches = []
    try:
        regex = re.compile(query, re.IGNORECASE)
        for root, _, files in os.walk(VAULT_DIR):
            for f in files:
                if f.endswith(".md"):
                    p = os.path.join(root, f)
                    try:
                        with open(p, "r", encoding="utf-8", errors="ignore") as fobj:
                            for i, line in enumerate(fobj):
                                if regex.search(line):
                                    rel = os.path.relpath(p, VAULT_DIR)
                                    matches.append(f"{rel}:{i+1}: {line.strip()}")
                                    if len(matches) > 100:
                                        matches.append("... [too many matches, truncated]")
                                        return "\n".join(matches)
                    except Exception:
                        pass
        return "\n".join(matches) if matches else "No matches found."
    except Exception as e:
        return f"Error in grep: {e}"

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
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file (relative to vault)"}},
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
            "description": "Search for a regex string inside all markdown files in the vault.",
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
                    "sections": {"type": "integer"}
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


def make_client():
    """Build the OpenAI-compatible client from LLM_* env vars, or raise ConfigError."""
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL")
    if not all([api_key, base_url, model]):
        raise ConfigError("LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL must be set for custom agent.")
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

    for _ in range(max_loops):
        if cancel_event is not None and cancel_event.is_set():
            raise AgentCancelled(extractor.text)

        if use_stream:
            msg, calls, cancelled = _stream_completion(
                client, model, messages, tools, cancel_event, extractor, usage_out)
            if cancelled:
                # Discard the aborted completion's partial tool calls; keep the
                # partial answer as plain assistant text so the history stays valid.
                if extractor.text:
                    messages.append({"role": "assistant", "content": extractor.text})
                raise AgentCancelled(extractor.text)
            messages.append(msg)
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
            _add_usage(usage_out, getattr(response, "usage", None))
            msg = response.choices[0].message
            messages.append(msg)
            calls = [(tc.id, tc.function.name, tc.function.arguments)
                     for tc in (msg.tool_calls or [])]

        if not calls:
            # Model responded with text instead of finishing. Just prompt it to finish.
            messages.append({"role": "user", "content": "Please continue and use the finish tool when done."})
            continue

        for pos, (call_id, func_name, raw_args) in enumerate(calls):
            try:
                args = json.loads(raw_args)
            except Exception as e:
                error_msg = f"Error parsing JSON arguments: {e}"
                print(f">> Tool Call: {func_name} - {error_msg} (raw: {raw_args!r})",
                      file=sys.stderr)
                messages.append({"role": "tool", "tool_call_id": call_id, "name": func_name,
                                 "content": error_msg})
                continue

            print(f">> Tool Call: {func_name}({_summarize_args(args)})", file=sys.stderr)
            if progress_cb is not None:
                try:
                    progress_cb(func_name, args)
                except Exception:
                    pass  # progress reporting must never break the agent loop

            if func_name == "finish":
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
                print(f"   !! {func_name} returned an error: {result}", file=sys.stderr)

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": func_name,
                "content": str(result)
            })

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
    if os.path.exists(filing_rules_path):
        with open(filing_rules_path, "r", encoding="utf-8") as f:
            system_content += "\n\nVault conventions (Filing_Rules.md):\n\n" + f.read()

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt}
    ]

    print(f"Starting custom agent loop with model {model} (DRY_RUN={DRY_RUN})...", file=sys.stderr)

    handlers = {
        "read_file": lambda a: read_file(a.get("path", "")),
        "glob_search": lambda a: glob_search(a.get("pattern", "")),
        "grep_search": lambda a: grep_search(a.get("query", "")),
        "write_file": lambda a: write_file(a.get("path", ""), a.get("content", "")),
        "edit_file": lambda a: edit_file(a.get("path", ""), a.get("old_string", ""), a.get("new_string", "")),
    }

    try:
        finish_args = run_loop(client, model, messages, TOOLS, handlers)
    except AgentAPIError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if finish_args is None:
        print(json.dumps({"error": "Exceeded maximum tool call loops"}))
        sys.exit(1)
    print(json.dumps(finish_args))

if __name__ == "__main__":
    main()
