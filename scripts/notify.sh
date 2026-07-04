#!/usr/bin/env bash
# Usage: notify.sh <title> <message> [priority] [tags]
# priority: min|low|default|high|urgent (default: "default")
# tags: comma-separated ntfy emoji tags, e.g. "white_check_mark" or "x"
set -euo pipefail

TITLE="${1:-Copilot Digest}"
MESSAGE="${2:-}"
PRIORITY="${3:-default}"
TAGS="${4:-}"

if [ -z "${NTFY_TOPIC:-}" ]; then
    echo "notify.sh: NTFY_TOPIC not set, skipping notification: $TITLE - $MESSAGE" >&2
    exit 0
fi

curl -fsS \
    -H "Title: ${TITLE}" \
    -H "Priority: ${PRIORITY}" \
    ${TAGS:+-H "Tags: ${TAGS}"} \
    --data-raw "${MESSAGE}" \
    "${NTFY_TOPIC}" >/dev/null || echo "notify.sh: failed to POST to NTFY_TOPIC" >&2
