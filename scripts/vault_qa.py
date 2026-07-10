"""Shared read-only Q&A logic for the vault, used by both ask_vault.py (CLI) and
vault_web.py (persistent chat server). Deliberately exposes no write/edit tools,
regardless of DRY_RUN or any other input - this module cannot modify the vault.
"""
import os
from collections import Counter
from datetime import datetime

import custom_agent_loop as cal
import lexical_index

READONLY_TOOL_NAMES = {"read_file", "glob_search", "grep_search"}

# Notes modified within this many days are tagged with their date in the index,
# so the model can spot likely-relevant recent notes. Annotating every path would
# add thousands of tokens to every turn for little gain.
_RECENT_DAYS = 30

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "Call this when you have the answer, to end the session.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "The answer to the user's question, in markdown."},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Vault-relative paths of the notes the answer was drawn from."
                },
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Vault-relative path of the cited note."},
                            "snippet": {"type": "string", "description": "Short verbatim excerpt (<=300 chars) the fact was drawn from."}
                        },
                        "required": ["path", "snippet"]
                    },
                    "description": ("Numbered by position: a [1] marker in the answer refers to "
                                    "citations[0], [2] to citations[1], and so on.")
                },
                "followups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-3 short follow-up questions the user might plausibly ask next."
                }
            },
            "required": ["answer"]
        }
    }
}

# Re-exported so vault_qa callers keep working; the canonical schema lives in
# lexical_index (shared with the ingestion agent and CLI).
SEARCH_RELEVANT_TOOL = lexical_index.SEARCH_RELEVANT_TOOL


def build_vault_index(vault_dir=None):
    """(path, mtime) for every note, same exclusions as the nightly index.
    Sorted by path. mtime lets the system prompt flag recently-modified notes and
    lets len() still report the note count."""
    vault_dir = vault_dir or cal.VAULT_DIR
    entries = []
    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in ("Attachments", "smart-chats"))
        for f in sorted(files):
            if f.endswith(".md"):
                abs_path = os.path.join(root, f)
                try:
                    mtime = os.path.getmtime(abs_path)
                except OSError:
                    mtime = 0.0
                entries.append((os.path.relpath(abs_path, vault_dir), mtime))
    return entries


def _index_lines(index, now):
    """One line per note; notes modified within _RECENT_DAYS get a date tag."""
    cutoff = now - _RECENT_DAYS * 86400
    lines = []
    for path, mtime in index:
        if mtime and mtime >= cutoff:
            lines.append(f"{path}  [modified {datetime.fromtimestamp(mtime):%Y-%m-%d}]")
        else:
            lines.append(path)
    return lines


def _folder_cheatsheet(index):
    """Top-level folder -> note count, e.g. 'Work (506), Daily jounal (600), ...',
    so the model knows where each kind of note lives without guessing."""
    counts = Counter(
        (p.split("/", 1)[0] if "/" in p.replace("\\", "/") else "(root)")
        for p, _ in ((path.replace("\\", "/"), m) for path, m in index)
    )
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{name} ({n})" for name, n in ordered)


def build_system_prompt(index, lexical_index=None):
    lexical_hint = (
        " search_relevant for thematic/paraphrased questions, and"
        if lexical_index is not None else ""
    )
    now = datetime.now()
    return (
        f"Current date/time: {now:%Y-%m-%d %H:%M} ({now:%A}), timezone Asia/Singapore "
        "(as of this session's start). Use it to resolve relative dates like "
        "\"today\", \"last week\", \"this month\".\n\n"
        "You are a read-only research assistant for a personal Obsidian vault. "
        "Answer the user's question from the vault's actual contents: shortlist candidate "
        f"notes from the index below by title and folder, Read the promising ones, use{lexical_hint} "
        "grep_search for identifiers (ticket numbers, names) that titles won't surface. "
        "Before citing a note you MUST read it (read_file) - never cite from the index or a "
        "search snippet alone. If a search returns nothing useful, try 2-3 alternate "
        "phrasings/synonyms and grep for identifiers before concluding the vault has no "
        "answer. Quote specifics (dates, ticket numbers, decisions) rather than generalities; "
        "for time-sensitive facts, state the note's date alongside the fact. Say plainly when "
        "the vault doesn't contain an answer. When done, call finish with the answer and the "
        "source note paths. Where the answer states a specific fact taken from a note, put a "
        "numeric marker like [1] right after it and supply a matching entry in finish's "
        "citations array (note path + the exact verbatim snippet the fact came from), numbered "
        "in order of first appearance. Also supply 2-3 short followups: natural next questions "
        "the user might ask about this vault.\n\n"
        "Vault conventions: daily notes live in 'Daily jounal/' named "
        "'YYYY-MM-DD (MMM) Weekday.md'; work notes live under 'Work/<Client>/<Function>/'. "
        "A '[modified YYYY-MM-DD]' tag on an index entry means the note was touched recently "
        "and is a likely candidate for questions about recent activity.\n"
        f"Top-level folders (note counts): {_folder_cheatsheet(index)}\n\n"
        f"Vault index ({len(index)} notes):\n" + "\n".join(_index_lines(index, now.timestamp()))
    )


def build_tools(lexical_index=None):
    tools = [t for t in cal.TOOLS if t["function"]["name"] in READONLY_TOOL_NAMES] + [FINISH_TOOL]
    if lexical_index is not None:
        tools.append(SEARCH_RELEVANT_TOOL)
    return tools


def build_handlers(lexical_index=None):
    handlers = {
        "read_file": lambda a: cal.read_file(a.get("path", ""), a.get("offset", 0), a.get("limit")),
        "glob_search": lambda a: cal.glob_search(a.get("pattern", "")),
        "grep_search": lambda a: cal.grep_search(a.get("query", "")),
    }
    if lexical_index is not None:
        handlers["search_relevant"] = lambda a: lexical_index.search(
            a.get("query", ""), top_k=a.get("top_k") or 8,
            path_prefix=a.get("path_prefix"),
            date_from=a.get("date_from"), date_to=a.get("date_to"))
    return handlers
