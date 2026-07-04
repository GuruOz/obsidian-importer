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
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
DRY_RUN="${DRY_RUN:-1}"
CLAUDE_MAX_BUDGET_USD="${CLAUDE_MAX_BUDGET_USD:-1.50}"
AGENT_ENGINE="${AGENT_ENGINE:-claude}"

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
    # No Edit in dry-run: Write is needed only for staging/proposed.md
    ALLOWED_TOOLS="Read,Glob,Grep,Write"
    # Remove any stale proposal: Agent's overwrite protection blocks writing a
    # pre-existing file the session hasn't read, which cost a whole failed run once.
    rm -f "${STAGING_DIR}/proposed.md"
else
    PROMPT_FILE="/app/prompt_template.txt"
    ALLOWED_TOOLS="Read,Glob,Grep,Edit,Write"
fi

AGENT_LOG="${LOG_DIR}/agent.${TODAY}.json"

# Pre-build a complete index of vault note paths. Claude uses it to shortlist
# create-vs-update candidates by title in one read, instead of burning turns (and
# tokens) on exploratory vault-wide searches - cheaper AND more complete coverage.
# Attachments (OneNote-import artifacts) and smart-chats are never filing targets.
(cd "$VAULT_DIR" && find . -name '*.md' \
    -not -path './.*' \
    -not -path './Attachments/*' \
    -not -path './smart-chats/*' \
    | sed 's|^\./||' | sort) > "${STAGING_DIR}/vault_index.txt"

if [ "$AGENT_ENGINE" = "custom" ]; then
    log "Invoking Custom Agent (LLM_MODEL: ${LLM_MODEL:-unknown})..."
    set +e
    python3 "/app/scripts/custom_agent_loop.py" "$PROMPT_FILE" > "$AGENT_LOG" 2>&1
    AGENT_RC=$?
    set -e
else
    # --add-dir: digest.md (and proposed.md in dry-run) live outside the vault cwd
    CLAUDE_ARGS=(-p "$(cat "$PROMPT_FILE")"
        --allowedTools "$ALLOWED_TOOLS"
        --add-dir "$STAGING_DIR"
        --max-budget-usd "$CLAUDE_MAX_BUDGET_USD"
        --output-format json)

    if [ -n "${CLAUDE_MODEL:-}" ]; then
        CLAUDE_ARGS+=(--model "$CLAUDE_MODEL")
    fi
    if [ -n "${CLAUDE_FALLBACK_MODEL:-}" ]; then
        CLAUDE_ARGS+=(--fallback-model "$CLAUDE_FALLBACK_MODEL")
    fi
    if [ -n "${CLAUDE_EFFORT:-}" ]; then
        CLAUDE_ARGS+=(--effort "$CLAUDE_EFFORT")
    fi

    log "Invoking Claude Code Agent..."
    set +e
    (cd "$VAULT_DIR" && claude "${CLAUDE_ARGS[@]}") > "$AGENT_LOG" 2>&1
    AGENT_RC=$?
    set -e
fi

if [ "$AGENT_RC" -ne 0 ]; then
    log "ERROR: Agent exited $AGENT_RC, see $AGENT_LOG"
    "$NOTIFY" "Copilot Digest FAILED" "Agent filing step failed (exit $AGENT_RC). See $AGENT_LOG." high x
    exit 1
fi

COST="$(grep -Eo '"total_cost_usd"[[:space:]]*:[[:space:]]*[0-9.]*' "$AGENT_LOG" | head -1 | awk -F: '{print $2}' | tr -d ' ')"
COST="${COST:-unknown}"

if [ "$DRY_RUN" = "1" ]; then
    # Don't trust the exit code alone - verify the deliverable actually landed
    # (we deleted any stale copy above, so existence means freshly written).
    if [ ! -s "${STAGING_DIR}/proposed.md" ]; then
        log "ERROR: Agent exited 0 but proposed.md was not written (cost: \$${COST})"
        "$NOTIFY" "Copilot Digest FAILED" "Dry run finished without writing proposed.md. See $AGENT_LOG." high x
        exit 1
    fi
    log "Dry-run complete. Proposal written to ${STAGING_DIR}/proposed.md (cost: \$${COST})"
    "$NOTIFY" "Copilot Digest (dry run)" "Proposal ready in staging/proposed.md. Cost: \$${COST}" default
else
    ARCHIVE_DIR="${VAULT_DIR}/Raw Digests"
    mkdir -p "$ARCHIVE_DIR"
    cp "${STAGING_DIR}/digest.md" "${ARCHIVE_DIR}/Copilot Digest - ${TODAY}.md"
    log "Archived raw digest to Raw Digests/Copilot Digest - ${TODAY}.md"

    log "Filing complete (cost: \$${COST})"
    "$NOTIFY" "Copilot Digest filed" "Tonight's digest was filed into the vault. Cost: \$${COST}" default white_check_mark
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
