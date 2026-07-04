#!/usr/bin/env python3
"""Ask questions about the Obsidian vault - a read-only Q&A agent.

Usage (from the host):
    ./ask.sh "what did I do on ticket CS0012345?"
    docker compose exec pipeline python3 scripts/ask_vault.py "summarize my Purview work this month"

Reuses the tool implementations from custom_agent_loop.py but exposes only the
read-only ones (read_file, glob_search, grep_search) - this agent cannot write or
edit anything, regardless of DRY_RUN.
"""
import json
import os
import sys

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


def build_vault_index():
    """Vault-relative paths of every note, same exclusions as the nightly index."""
    paths = []
    for root, dirs, files in os.walk(cal.VAULT_DIR):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in ("Attachments", "smart-chats"))
        for f in sorted(files):
            if f.endswith(".md"):
                paths.append(os.path.relpath(os.path.join(root, f), cal.VAULT_DIR))
    return paths


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('usage: ask_vault.py "your question"', file=sys.stderr)
        sys.exit(2)
    question = " ".join(sys.argv[1:]).strip()

    client, model = cal.make_client()

    index = build_vault_index()
    system_content = (
        "You are a read-only research assistant for a personal Obsidian vault. "
        "Answer the user's question from the vault's actual contents: shortlist candidate "
        "notes from the index below by title and folder, Read the promising ones, and use "
        "grep_search for identifiers (ticket numbers, names) that titles won't surface. "
        "Quote specifics (dates, ticket numbers, decisions) rather than generalities, and "
        "say plainly when the vault doesn't contain an answer. When done, call finish with "
        "the answer and the source note paths.\n\n"
        f"Vault index ({len(index)} notes):\n" + "\n".join(index)
    )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": question},
    ]

    tools = [t for t in cal.TOOLS if t["function"]["name"] in READONLY_TOOL_NAMES] + [FINISH_TOOL]
    handlers = {
        "read_file": lambda a: cal.read_file(a.get("path", "")),
        "glob_search": lambda a: cal.glob_search(a.get("pattern", "")),
        "grep_search": lambda a: cal.grep_search(a.get("query", "")),
    }

    print(f"Searching vault ({len(index)} notes) with {model}...", file=sys.stderr)
    finish_args = cal.run_loop(client, model, messages, tools, handlers, max_loops=40)

    if finish_args is None:
        print("ERROR: agent did not produce an answer within the loop limit.", file=sys.stderr)
        sys.exit(1)

    print(finish_args.get("answer", "").strip())
    sources = finish_args.get("sources") or []
    if sources:
        print("\nSources:")
        for s in sources:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
