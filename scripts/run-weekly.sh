#!/usr/bin/env bash
# Weekly rollup orchestrator. Fired Sunday evenings by supercronic (see /app/crontab),
# or run manually with: docker compose exec pipeline /app/scripts/run-weekly.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${SCRIPT_DIR}/notify.sh"

VAULT_DIR="${VAULT_DIR:-/vault}"
WORKDIR="${WORKDIR:-/work}"
LOG_DIR="${LOG_DIR:-/work/logs}"
STAGING_DIR="${STAGING_DIR:-/work/staging}"
DRY_RUN="${DRY_RUN:-1}"

mkdir -p "$LOG_DIR" "$STAGING_DIR"

LOCK_FILE="${WORKDIR}/run-weekly.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "run-weekly.sh: another run is already in progress, exiting." >&2
    exit 0
fi

TODAY="$(date +%F)"
DIGEST_LOG="${LOG_DIR}/digest.log"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$DIGEST_LOG"
}

# The rollup creates a real vault note, which the agent's write guard blocks in
# dry-run mode - skip cleanly instead of failing every Sunday of the M3 period.
if [ "$DRY_RUN" = "1" ]; then
    log "Weekly rollup skipped: DRY_RUN=1 (vault writes are disabled)."
    exit 0
fi

# ISO week id and the seven dates ending today.
WEEK_ID="$(date +%G-W%V)"
{
    echo "ISO week: ${WEEK_ID}"
    echo "Dates covered (oldest first):"
    for i in 6 5 4 3 2 1 0; do
        date -d "-${i} days" +%F
    done
} > "${STAGING_DIR}/weekly_context.txt"

# Same complete note index the nightly run uses (same exclusions).
(cd "$VAULT_DIR" && find . -name '*.md' \
    -not -path './.*' \
    -not -path './Attachments/*' \
    -not -path './smart-chats/*' \
    | sed 's|^\./||' | sort) > "${STAGING_DIR}/vault_index.txt"

AGENT_LOG="${LOG_DIR}/weekly.${TODAY}.json"

log "Invoking weekly rollup agent for ${WEEK_ID} (LLM_MODEL: ${LLM_MODEL:-unknown})..."
set +e
python3 "${SCRIPT_DIR}/custom_agent_loop.py" /app/prompt_weekly_rollup.txt > "$AGENT_LOG" 2>&1
AGENT_RC=$?
set -e

if [ "$AGENT_RC" -ne 0 ]; then
    log "ERROR: Weekly rollup agent exited $AGENT_RC, see $AGENT_LOG"
    "$NOTIFY" "Weekly Review FAILED" "Rollup agent failed (exit $AGENT_RC). See $AGENT_LOG." high x
    exit 1
fi

REPORT="$(python3 "${SCRIPT_DIR}/filing_report.py" "$AGENT_LOG" \
    --vault-log "${VAULT_DIR}/Raw Digests/Filing Log.md" || true)"
log "Weekly rollup complete (${REPORT:-no summary})"
"$NOTIFY" "Weekly Review ready" "${REPORT:-Weekly review note written for ${WEEK_ID}.}" default white_check_mark

# This container runs as root; the dockerized Obsidian client runs as
# PUID=1000/PGID=1000, so a root-owned note it just wrote would EACCES
# Obsidian's own sync process (same fix as run-ingest.sh).
chown -R 1000:1000 "$VAULT_DIR" 2>&1 | tee -a "$DIGEST_LOG" || true

find "$LOG_DIR" -name 'weekly.*.json' -type f -mtime +90 -exec rm -f {} \;
