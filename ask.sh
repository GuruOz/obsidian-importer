#!/usr/bin/env bash
# Ask the vault a question: ./ask.sh "what did I do on ticket CS0012345?"
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ $# -eq 0 ]; then
    echo 'usage: ./ask.sh "your question"' >&2
    exit 2
fi

docker compose exec -T pipeline python3 /app/scripts/ask_vault.py "$*"
