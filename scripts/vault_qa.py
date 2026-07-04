"""Shared read-only Q&A logic for the vault, used by both ask_vault.py (CLI) and
vault_web.py (persistent chat server). Deliberately exposes no write/edit tools,
regardless of DRY_RUN or any other input - this module cannot modify the vault.
"""
import os

import custom_agent_loop as cal

READONLY_TOOL_NAMES = {"read_file", "glob_search", "grep_search"}

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
                }
            },
            "required": ["answer"]
        }
    }
}

SEARCH_RELEVANT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_relevant",
        "description": (
            "BM25 lexical search over chunked vault text - use for thematic/paraphrased "
            "questions where you don't know the exact wording. Use grep_search instead "
            "for exact identifiers (ticket numbers, exact names/dates)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "description": "Number of chunks to return (default 8)."}
            },
            "required": ["query"]
        }
    }
}


def build_vault_index(vault_dir=None):
    """Vault-relative paths of every note, same exclusions as the nightly index."""
    vault_dir = vault_dir or cal.VAULT_DIR
    paths = []
    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in ("Attachments", "smart-chats"))
        for f in sorted(files):
            if f.endswith(".md"):
                paths.append(os.path.relpath(os.path.join(root, f), vault_dir))
    return paths


def build_system_prompt(index, lexical_index=None):
    lexical_hint = (
        " search_relevant for thematic/paraphrased questions, and"
        if lexical_index is not None else ""
    )
    return (
        "You are a read-only research assistant for a personal Obsidian vault. "
        "Answer the user's question from the vault's actual contents: shortlist candidate "
        f"notes from the index below by title and folder, Read the promising ones, use{lexical_hint} "
        "grep_search for identifiers (ticket numbers, names) that titles won't surface. "
        "Quote specifics (dates, ticket numbers, decisions) rather than generalities, and "
        "say plainly when the vault doesn't contain an answer. When done, call finish with "
        "the answer and the source note paths.\n\n"
        f"Vault index ({len(index)} notes):\n" + "\n".join(index)
    )


def build_tools(lexical_index=None):
    tools = [t for t in cal.TOOLS if t["function"]["name"] in READONLY_TOOL_NAMES] + [FINISH_TOOL]
    if lexical_index is not None:
        tools.append(SEARCH_RELEVANT_TOOL)
    return tools


def build_handlers(lexical_index=None):
    handlers = {
        "read_file": lambda a: cal.read_file(a.get("path", "")),
        "glob_search": lambda a: cal.glob_search(a.get("pattern", "")),
        "grep_search": lambda a: cal.grep_search(a.get("query", "")),
    }
    if lexical_index is not None:
        handlers["search_relevant"] = lambda a: lexical_index.search(
            a.get("query", ""), top_k=a.get("top_k") or 8)
    return handlers
