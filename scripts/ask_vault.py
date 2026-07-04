#!/usr/bin/env python3
"""Ask questions about the Obsidian vault - a read-only Q&A agent.

Usage (from the host):
    ./ask.sh "what did I do on ticket CS0012345?"
    docker compose exec pipeline python3 scripts/ask_vault.py "summarize my Purview work this month"

Thin CLI wrapper over vault_qa.py, which also backs the persistent web chat
(vault_web.py) - the index/prompt/tool logic lives there so both stay in sync.
Reuses only the read-only tool implementations from custom_agent_loop.py
(read_file, glob_search, grep_search) plus a lexical (BM25) relevance search -
this agent cannot write or edit anything, regardless of DRY_RUN.
"""
import sys

import custom_agent_loop as cal
import lexical_index
import vault_qa


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('usage: ask_vault.py "your question"', file=sys.stderr)
        sys.exit(2)
    question = " ".join(sys.argv[1:]).strip()

    try:
        client, model = cal.make_client()
    except cal.ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    index = vault_qa.build_vault_index()
    lex = lexical_index.build_index(cal.VAULT_DIR)
    system_content = vault_qa.build_system_prompt(index, lexical_index=lex)

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": question},
    ]

    tools = vault_qa.build_tools(lexical_index=lex)
    handlers = vault_qa.build_handlers(lexical_index=lex)

    print(f"Searching vault ({len(index)} notes) with {model}...", file=sys.stderr)
    try:
        finish_args = cal.run_loop(client, model, messages, tools, handlers, max_loops=40)
    except cal.AgentAPIError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

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
