#!/usr/bin/env bash
# Generic ingestion orchestrator. One source per invocation:
#
#   run-ingest.sh <source> [YYYY-MM-DD]
#
# A "source" is three things: a fetcher that stages markdown + pending_ids.txt, a
# prompt template (live + dry-run), and a handful of env vars. Adding a future
# source (calendar, WhatsApp export, bookmarks, ...) means: add a case entry
# below, drop in a fetcher, and write a prompt - the orchestration (vault lock,
# snapshot, exit-code contract, dry-run verification, raw archive, deferred
# ledger commit, notify, prune) is shared here.
#
# Per-source config comes from env with a prefix convention: for source
# `personal` the prefix is PERSONAL_MAIL_, so PERSONAL_MAIL_DRY_RUN,
# PERSONAL_MAIL_STAGING_DIR, PERSONAL_MAIL_LEDGER_FILE, and optionally
# PERSONAL_MAIL_LLM_MODEL / PERSONAL_MAIL_LLM_BASE_URL / PERSONAL_MAIL_LLM_API_KEY
# override the defaults - so e.g. personal mail can dry-run for a week while the
# digest keeps filing live, or point at a different (even local) model.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${SCRIPT_DIR}/notify.sh"

SOURCE="${1:-}"
if [ -z "$SOURCE" ]; then
    echo "usage: run-ingest.sh <source> [YYYY-MM-DD]" >&2
    exit 1
fi
shift
WORK_DATE_ARG="${1:-}"

# --- Per-source identity + defaults (the only place a new source is declared) ---
case "$SOURCE" in
    digest)
        PREFIX="DIGEST"
        SRC_TITLE="Copilot Digest"
        FETCHER="fetch_digest.py"
        PROMPT_LIVE="/app/prompt_template.txt"
        PROMPT_DRY="/app/prompt_dry_run.txt"
        STAGED_FILE="digest.md"
        ARCHIVE_SUBDIR="Raw Digests"
        ARCHIVE_LABEL="Copilot Digest"
        # Digest preserves its historical paths, inheriting the unprefixed globals
        # (and the global DRY_RUN) so the existing .env keeps working untouched.
        DEF_STAGING="${STAGING_DIR:-/work/staging}"
        DEF_LEDGER="${LEDGER_FILE:-/work/processed_ids.txt}"
        DEF_DRY_RUN="${DRY_RUN:-1}"
        # Real multi-workstream digests regularly exhaust 30 turns (the agent
        # then never calls finish and the run logs as "unreported").
        DEF_MAX_LOOPS=60
        ;;
    personal)
        PREFIX="PERSONAL_MAIL"
        SRC_TITLE="Personal Email"
        FETCHER="fetch_inbox.py"
        PROMPT_LIVE="/app/prompt_personal_email.txt"
        PROMPT_DRY="/app/prompt_personal_email_dry_run.txt"
        STAGED_FILE="personal.md"
        ARCHIVE_SUBDIR="Raw Email"
        ARCHIVE_LABEL="Personal Mail"
        # A new source defaults to its own staging/ledger and to DRY_RUN=1
        # (safe): it deliberately does NOT inherit the global DRY_RUN, so going
        # the digest live never silently takes personal mail live with it.
        DEF_STAGING="/work/staging/personal"
        DEF_LEDGER="/work/personal_processed_ids.txt"
        DEF_DRY_RUN="1"
        DEF_MAX_LOOPS=30
        ;;
    *)
        echo "run-ingest.sh: unknown source '$SOURCE' (known: digest, personal)." >&2
        exit 1
        ;;
esac

# pref SUFFIX DEFAULT -> value of ${PREFIX}_SUFFIX if set, else DEFAULT.
pref() {
    local var="${PREFIX}_$1"
    printf '%s' "${!var:-$2}"
}

# Resolve and export the per-source values the fetcher and agent read from env.
VAULT_DIR="${VAULT_DIR:-/vault}"
WORKDIR="${WORKDIR:-/work}"
LOG_DIR="${LOG_DIR:-/work/logs}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

STAGING_DIR="$(pref STAGING_DIR "$DEF_STAGING")"
LEDGER_FILE="$(pref LEDGER_FILE "$DEF_LEDGER")"
DRY_RUN="$(pref DRY_RUN "$DEF_DRY_RUN")"
FILING_LOG="$(pref FILING_LOG "${VAULT_DIR}/${ARCHIVE_SUBDIR}/Filing Log.md")"
# Resolved once here and exported, so the fetcher's first-run write and this
# script's deferred commit target the exact same file regardless of WORKDIR.
WATERMARK_FILE="$(pref WATERMARK_FILE "${WORKDIR}/${SOURCE}_watermark.txt")"

# Optional per-source LLM endpoint override (default: the global LLM_* vars).
LLM_MODEL="$(pref LLM_MODEL "${LLM_MODEL:-}")"
LLM_BASE_URL="$(pref LLM_BASE_URL "${LLM_BASE_URL:-}")"
LLM_API_KEY="$(pref LLM_API_KEY "${LLM_API_KEY:-}")"

# Agent loop budget, tunable per source (DIGEST_MAX_LOOPS / PERSONAL_MAIL_MAX_LOOPS,
# global INGEST_MAX_LOOPS fallback, then the per-source default declared above);
# custom_agent_loop.py reads AGENT_MAX_LOOPS.
AGENT_MAX_LOOPS="$(pref MAX_LOOPS "${INGEST_MAX_LOOPS:-$DEF_MAX_LOOPS}")"

export VAULT_DIR WORKDIR STAGING_DIR LEDGER_FILE DRY_RUN WATERMARK_FILE \
    LLM_MODEL LLM_BASE_URL LLM_API_KEY AGENT_MAX_LOOPS

# Optional work-date override (backfilling a specific day), same contract as before.
WORK_DATE="${WORK_DATE_ARG:-${WORK_DATE:-}}"
if [ -n "$WORK_DATE" ] && ! [[ "$WORK_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "run-ingest.sh: invalid work date '$WORK_DATE' - expected YYYY-MM-DD." >&2
    exit 1
fi
export WORK_DATE

mkdir -p "$LOG_DIR" "$STAGING_DIR" "$WORKDIR/backups"

# --- Shared vault lock: every source serializes on the same lock, so two sources
# (e.g. the nightly digest and personal mail) never touch the vault concurrently. ---
LOCK_FILE="${WORKDIR}/ingest.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "run-ingest.sh: another ingestion run is already in progress, exiting." >&2
    exit 0
fi

TODAY="$(date +%F)"
DIGEST_LOG="${LOG_DIR}/digest.log"

log() {
    echo "[$(date -Iseconds)] [${SOURCE}] $*" | tee -a "$DIGEST_LOG"
}

RUN_START_TS=$(date +%s)
log "=== run start: source=${SOURCE} dry_run=${DRY_RUN} staging=${STAGING_DIR} ledger=${LEDGER_FILE} watermark_file=${WATERMARK_FILE}${WORK_DATE:+ work_date=${WORK_DATE}} ==="

# --- Health check: is the Obsidian sync client up? Writes still land even if not,
# but sync to other devices will be delayed until it comes back. Only runs when
# OBSIDIAN_HEALTHCHECK_URL is set - e.g. not on a machine where a native Obsidian
# desktop app (outside Docker) already handles sync for a bind-mounted vault. ---
if [ -n "${OBSIDIAN_HEALTHCHECK_URL:-}" ] && ! curl -fsS -o /dev/null "$OBSIDIAN_HEALTHCHECK_URL"; then
    log "WARNING: obsidian container not responding at $OBSIDIAN_HEALTHCHECK_URL"
    "$NOTIFY" "$SRC_TITLE" "Obsidian sync client appears down - writes will queue until it's back up." low warning
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
# Fetcher output is tee'd into the persistent run log so triage decisions
# (staged/skipped per email) survive in the Logs tab, not just in cron.log.
log "Fetching via ${FETCHER}..."
set +e
python3 "${SCRIPT_DIR}/${FETCHER}" 2>&1 | tee -a "$DIGEST_LOG"
FETCH_RC=${PIPESTATUS[0]}
set -e
log "Fetcher finished with exit ${FETCH_RC} after $(($(date +%s) - RUN_START_TS))s"

if [ "$FETCH_RC" -eq 20 ]; then
    log "Nothing new to ingest."
    "$NOTIFY" "$SRC_TITLE" "Nothing new to ingest - nothing to file." default
    exit 0
elif [ "$FETCH_RC" -eq 30 ]; then
    log "ERROR: Graph auth failed (exit 30). Re-run graph_auth.py."
    "$NOTIFY" "$SRC_TITLE FAILED" "Graph auth expired/missing - run graph_auth.py again." high x
    exit 1
elif [ "$FETCH_RC" -ne 0 ]; then
    log "ERROR: ${FETCHER} failed with exit $FETCH_RC"
    "$NOTIFY" "$SRC_TITLE FAILED" "${FETCHER} failed (exit $FETCH_RC). See logs." high x
    exit 1
fi

# --- Agent filing step ---
if [ "$DRY_RUN" = "1" ]; then
    PROMPT_FILE="$PROMPT_DRY"
    # Remove any stale proposal so its existence after the run proves it was
    # freshly written by tonight's agent.
    rm -f "${STAGING_DIR}/proposed.md"
else
    PROMPT_FILE="$PROMPT_LIVE"
fi

AGENT_LOG="${LOG_DIR}/agent.${SOURCE}.${TODAY}.json"

# Pre-build a complete index of vault note paths. The agent uses it to shortlist
# create-vs-update candidates by title in one read, instead of burning turns (and
# tokens) on exploratory vault-wide searches - cheaper AND more complete coverage.
# Attachments (OneNote-import artifacts) and smart-chats are never filing targets.
# Each line is "YYYY-MM-DD<TAB>path" (the note's last-modified date); a recently
# modified topic note matching a workstream is the likely continuation target.
(cd "$VAULT_DIR" && find . -name '*.md' \
    -not -path './.*' \
    -not -path './Attachments/*' \
    -not -path './smart-chats/*' \
    -printf '%TY-%Tm-%Td\t%P\n' | sort -k2) > "${STAGING_DIR}/vault_index.txt"

# Stage the tail of this source's Filing Log so the agent can see where recent
# runs filed recurring workstreams and continue them in the same canonical note.
if [ -f "$FILING_LOG" ]; then
    tail -n 30 "$FILING_LOG" > "${STAGING_DIR}/recent_filing_log.md"
else
    rm -f "${STAGING_DIR}/recent_filing_log.md"
fi

log "Invoking agent (LLM_MODEL: ${LLM_MODEL:-unknown}, prompt: ${PROMPT_FILE}, log: ${AGENT_LOG})..."
AGENT_START_TS=$(date +%s)
# tee (not redirect): the full agent transcript lands in AGENT_LOG for the Logs
# tab / filing_report, AND streams to stdout so a manual run from the settings
# page shows the agent's activity live instead of going silent for minutes.
set +e
python3 "/app/scripts/custom_agent_loop.py" "$PROMPT_FILE" 2>&1 | tee "$AGENT_LOG"
AGENT_RC=${PIPESTATUS[0]}
set -e
log "Agent finished with exit ${AGENT_RC} after $(($(date +%s) - AGENT_START_TS))s"

if [ "$AGENT_RC" -ne 0 ]; then
    log "ERROR: Agent exited $AGENT_RC, see $AGENT_LOG"
    "$NOTIFY" "$SRC_TITLE FAILED" "Agent filing step failed (exit $AGENT_RC). See $AGENT_LOG." high x
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    # Don't trust the exit code alone - verify the deliverable actually landed
    # (we deleted any stale copy above, so existence means freshly written).
    if [ ! -s "${STAGING_DIR}/proposed.md" ]; then
        log "ERROR: Agent exited 0 but proposed.md was not written"
        "$NOTIFY" "$SRC_TITLE FAILED" "Dry run finished without writing proposed.md. See $AGENT_LOG." high x
        exit 1
    fi
    REPORT="$(python3 "${SCRIPT_DIR}/filing_report.py" "$AGENT_LOG" || true)"
    log "Dry-run complete. Proposal written to ${STAGING_DIR}/proposed.md (${REPORT:-no summary})"
    "$NOTIFY" "$SRC_TITLE (dry run)" "Proposal ready in ${STAGING_DIR}/proposed.md. ${REPORT}" default
else
    ARCHIVE_DIR="${VAULT_DIR}/${ARCHIVE_SUBDIR}"
    mkdir -p "$ARCHIVE_DIR"
    cp "${STAGING_DIR}/${STAGED_FILE}" "${ARCHIVE_DIR}/${ARCHIVE_LABEL} - ${TODAY}.md"
    log "Archived raw batch to ${ARCHIVE_SUBDIR}/${ARCHIVE_LABEL} - ${TODAY}.md"

    # Audit trail: summarize what the agent touched, both to ntfy and to a
    # wikilinked "Filing Log" note inside the vault (visible on every synced device).
    REPORT="$(python3 "${SCRIPT_DIR}/filing_report.py" "$AGENT_LOG" \
        --vault-log "$FILING_LOG" || true)"
    log "Filing complete (${REPORT:-no summary})"
    "$NOTIFY" "$SRC_TITLE filed" "${REPORT:-Batch was filed into the vault.}" default white_check_mark
fi

# --- Deterministic post-run verification (warn-only, never blocks the commit).
# Runs before the ledger commit so pending_ids.txt is still present. On a live run
# it appends a verify sub-line to the Filing Log entry filing_report just wrote. ---
VERIFY_ARGS=(--source "$SOURCE" --staging-dir "$STAGING_DIR" --vault-dir "$VAULT_DIR" \
    --agent-log "$AGENT_LOG" --filing-log "$FILING_LOG")
if [ "$DRY_RUN" = "1" ]; then VERIFY_ARGS+=(--dry-run); fi
python3 "${SCRIPT_DIR}/verify_filing.py" "${VERIFY_ARGS[@]}" 2>&1 | tee -a "$DIGEST_LOG" || true

# --- Fix ownership after any live vault write. This container runs as root (no
# USER in the Dockerfile), but the dockerized Obsidian client runs as
# PUID=1000/PGID=1000 (see docker-compose.yml), so files this container just
# created land root:root and Obsidian's own sync process gets EACCES trying to
# read/write/merge them. Cheap and idempotent - re-chowning unchanged files is
# a no-op. ---
if [ "$DRY_RUN" != "1" ]; then
    chown -R 1000:1000 "$VAULT_DIR" 2>&1 | tee -a "$DIGEST_LOG" || true
fi

# --- Commit the ledger only now that the run demonstrably succeeded. If anything
# above failed, the staged ids stay un-ledgered and tomorrow's fetch retries them. ---
if [ "$DRY_RUN" != "1" ]; then
    if [ -s "${STAGING_DIR}/pending_ids.txt" ]; then
        cat "${STAGING_DIR}/pending_ids.txt" >> "$LEDGER_FILE"
        rm -f "${STAGING_DIR}/pending_ids.txt"
        log "Ledger updated."
    fi

    # The fetcher may stage a watermark to advance only on success (same deferral as
    # the ledger); commit it now that the run succeeded.
    if [ -f "${STAGING_DIR}/pending_watermark.txt" ]; then
        mv "${STAGING_DIR}/pending_watermark.txt" "$WATERMARK_FILE"
        log "Watermark advanced."
    fi
else
    log "DRY_RUN enabled: skipping ledger and watermark commit."
    rm -f "${STAGING_DIR}/pending_ids.txt" "${STAGING_DIR}/pending_watermark.txt"
fi

# --- Maintenance: Prune old logs & archives ---
log "Pruning agent logs and raw archives older than 30 days..."
find "$LOG_DIR" -name 'agent.*.json' -type f -mtime +30 -exec rm -f {} \;
find "${STAGING_DIR}/archive" -name '*.md' -type f -mtime +30 -exec rm -f {} \; 2>/dev/null || true

# Rotate custom logs (keep last 5000 lines)
tail -n 5000 "$DIGEST_LOG" > "${DIGEST_LOG}.tmp" && mv "${DIGEST_LOG}.tmp" "$DIGEST_LOG"
if [ -f "/work/logs/cron.log" ]; then
    tail -n 5000 "/work/logs/cron.log" > "/work/logs/cron.log.tmp" && mv "/work/logs/cron.log.tmp" "/work/logs/cron.log"
fi

log "=== run complete: source=${SOURCE} total $(($(date +%s) - RUN_START_TS))s ==="
