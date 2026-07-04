#!/usr/bin/env bash
# Orchestrator entry point. Fired nightly by supercronic (see /app/crontab), or run
# manually with: docker compose exec pipeline /app/scripts/run-digest.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${SCRIPT_DIR}/notify.sh"

VAULT_DIR="${VAULT_DIR:-/vault}"
WORKDIR="${WORKDIR:-/work}"
LOG_DIR="${LOG_DIR:-/work/logs}"
STAGING_DIR="${STAGING_DIR:-/work/staging}"
LEDGER_FILE="${LEDGER_FILE:-/work/processed_ids.txt}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
DRY_RUN="${DRY_RUN:-1}"

mkdir -p "$LOG_DIR" "$STAGING_DIR" "$WORKDIR/backups"

LOCK_FILE="${WORKDIR}/run-digest.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "run-digest.sh: another run is already in progress, exiting." >&2
    exit 0
fi

TODAY="$(date +%F)"
DIGEST_LOG="${LOG_DIR}/digest.log"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$DIGEST_LOG"
}

# --- Health check: is the Obsidian sync client up? Writes still land even if not,
# but sync to other devices will be delayed until it comes back. Only runs when
# OBSIDIAN_HEALTHCHECK_URL is set - e.g. not on a machine where a native Obsidian
# desktop app (outside Docker) already handles sync for a bind-mounted vault. ---
if [ -n "${OBSIDIAN_HEALTHCHECK_URL:-}" ] && ! curl -fsS -o /dev/null "$OBSIDIAN_HEALTHCHECK_URL"; then
    log "WARNING: obsidian container not responding at $OBSIDIAN_HEALTHCHECK_URL"
    "$NOTIFY" "Copilot Digest" "Obsidian sync client appears down - writes will queue until it's back up." low warning
fi

# --- Pre-run safety snapshot (incremental: unchanged files are hard-linked to the
# previous day's snapshot, so each day only costs the space of what changed; if the
# filesystem can't hard-link, rsync silently falls back to full copies) ---
BACKUP_ROOT="${WORKDIR}/backups"
BACKUP_DIR="${BACKUP_ROOT}/${TODAY}"
PREV_BACKUP="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name '????-??-??' ! -name "$TODAY" | sort | tail -1)"
mkdir -p "$BACKUP_DIR"
if [ -n "$PREV_BACKUP" ]; then
    rsync -a --delete --link-dest="$PREV_BACKUP" "${VAULT_DIR}/" "${BACKUP_DIR}/"
else
    rsync -a --delete "${VAULT_DIR}/" "${BACKUP_DIR}/"
fi
log "Vault snapshot written to ${BACKUP_DIR}"

find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -mtime +"${BACKUP_RETENTION_DAYS}" -exec rm -rf {} \;

# --- Ingestion ---
set +e
python3 "${SCRIPT_DIR}/fetch_digest.py"
FETCH_RC=$?
set -e

if [ "$FETCH_RC" -eq 20 ]; then
    log "No new digest email today."
    "$NOTIFY" "Copilot Digest" "No digest email today - nothing to file." default
    exit 0
elif [ "$FETCH_RC" -eq 30 ]; then
    log "ERROR: Graph auth failed (exit 30). Re-run graph_auth.py."
    "$NOTIFY" "Copilot Digest FAILED" "Graph auth expired/missing - run graph_auth.py again." high x
    exit 1
elif [ "$FETCH_RC" -ne 0 ]; then
    log "ERROR: fetch_digest.py failed with exit $FETCH_RC"
    "$NOTIFY" "Copilot Digest FAILED" "fetch_digest.py failed (exit $FETCH_RC). See logs." high x
    exit 1
fi

# --- Agent filing step ---
if [ "$DRY_RUN" = "1" ]; then
    PROMPT_FILE="/app/prompt_dry_run.txt"
    # Remove any stale proposal so its existence after the run proves it was
    # freshly written by tonight's agent.
    rm -f "${STAGING_DIR}/proposed.md"
else
    PROMPT_FILE="/app/prompt_template.txt"
fi

AGENT_LOG="${LOG_DIR}/agent.${TODAY}.json"

# Pre-build a complete index of vault note paths. The agent uses it to shortlist
# create-vs-update candidates by title in one read, instead of burning turns (and
# tokens) on exploratory vault-wide searches - cheaper AND more complete coverage.
# Attachments (OneNote-import artifacts) and smart-chats are never filing targets.
(cd "$VAULT_DIR" && find . -name '*.md' \
    -not -path './.*' \
    -not -path './Attachments/*' \
    -not -path './smart-chats/*' \
    | sed 's|^\./||' | sort) > "${STAGING_DIR}/vault_index.txt"

log "Invoking agent (LLM_MODEL: ${LLM_MODEL:-unknown})..."
set +e
python3 "/app/scripts/custom_agent_loop.py" "$PROMPT_FILE" > "$AGENT_LOG" 2>&1
AGENT_RC=$?
set -e

if [ "$AGENT_RC" -ne 0 ]; then
    log "ERROR: Agent exited $AGENT_RC, see $AGENT_LOG"
    "$NOTIFY" "Copilot Digest FAILED" "Agent filing step failed (exit $AGENT_RC). See $AGENT_LOG." high x
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    # Don't trust the exit code alone - verify the deliverable actually landed
    # (we deleted any stale copy above, so existence means freshly written).
    if [ ! -s "${STAGING_DIR}/proposed.md" ]; then
        log "ERROR: Agent exited 0 but proposed.md was not written"
        "$NOTIFY" "Copilot Digest FAILED" "Dry run finished without writing proposed.md. See $AGENT_LOG." high x
        exit 1
    fi
    log "Dry-run complete. Proposal written to ${STAGING_DIR}/proposed.md"
    "$NOTIFY" "Copilot Digest (dry run)" "Proposal ready in staging/proposed.md." default
else
    ARCHIVE_DIR="${VAULT_DIR}/Raw Digests"
    mkdir -p "$ARCHIVE_DIR"
    cp "${STAGING_DIR}/digest.md" "${ARCHIVE_DIR}/Copilot Digest - ${TODAY}.md"
    log "Archived raw digest to Raw Digests/Copilot Digest - ${TODAY}.md"

    log "Filing complete"
    "$NOTIFY" "Copilot Digest filed" "Tonight's digest was filed into the vault." default white_check_mark
fi

# --- Commit the ledger only now that the run demonstrably succeeded. If anything
# above failed, the staged emails stay un-ledgered and tomorrow's fetch retries them. ---
if [ -s "${STAGING_DIR}/pending_ids.txt" ]; then
    cat "${STAGING_DIR}/pending_ids.txt" >> "$LEDGER_FILE"
    rm -f "${STAGING_DIR}/pending_ids.txt"
    log "Ledger updated."
fi

# --- Maintenance: Prune old logs & archives ---
log "Pruning agent logs and raw digests older than 30 days..."
find "$LOG_DIR" -name 'agent.*.json' -type f -mtime +30 -exec rm -f {} \;
find "${STAGING_DIR}/archive" -name '*.md' -type f -mtime +30 -exec rm -f {} \;

# Rotate custom logs (keep last 5000 lines)
tail -n 5000 "$DIGEST_LOG" > "${DIGEST_LOG}.tmp" && mv "${DIGEST_LOG}.tmp" "$DIGEST_LOG"
if [ -f "/work/logs/cron.log" ]; then
    tail -n 5000 "/work/logs/cron.log" > "/work/logs/cron.log.tmp" && mv "/work/logs/cron.log.tmp" "/work/logs/cron.log"
fi
