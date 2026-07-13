#!/usr/bin/env bash
# Nightly stitch pass: after all sources have filed (last cron job is WhatsApp at
# 23:30), correlate last night's filings ACROSS sources - cross-link related notes,
# report duplicate-note merge candidates, flag filing oddities. See plan.md.
#
# Fired at 05:30 SGT by supercronic (see /app/crontab), or manually:
#   docker compose exec pipeline /app/scripts/run-stitch.sh
#
# Modes (STITCH_APPLY_LINKS in .env):
#   0 (default)  report-only: the agent runs with DRY_RUN=1, so its only writable
#                file is staging/stitch/proposed.md - links are proposed, not applied.
#   1            live links: the agent may append "See also" lines to vault notes.
#                Note merges are NEVER applied in either mode, only proposed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${SCRIPT_DIR}/notify.sh"

VAULT_DIR="${VAULT_DIR:-/vault}"
WORKDIR="${WORKDIR:-/work}"
LOG_DIR="${LOG_DIR:-/work/logs}"
STAGING_DIR="${STITCH_STAGING_DIR:-/work/staging/stitch}"
APPLY_LINKS="${STITCH_APPLY_LINKS:-0}"
MAX_TOPICS="${STITCH_MAX_TOPICS:-20}"
AGENT_MAX_LOOPS="${STITCH_MAX_LOOPS:-40}"

# The agent loop's write guard keys off DRY_RUN: report-only mode blocks every
# write except staging proposed.md, exactly like the ingestion dry runs.
if [ "$APPLY_LINKS" = "1" ]; then
    PROMPT_FILE="/app/prompt_stitch.txt"
    DRY_RUN=0
else
    PROMPT_FILE="/app/prompt_stitch_dry_run.txt"
    DRY_RUN=1
fi
export VAULT_DIR WORKDIR STAGING_DIR DRY_RUN AGENT_MAX_LOOPS

mkdir -p "$LOG_DIR" "$STAGING_DIR"

# Same lock as every ingestion run: stitch never overlaps a filing run (and the
# dashboard's restart guard covers a running stitch for free).
LOCK_FILE="${WORKDIR}/ingest.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "run-stitch.sh: an ingestion run is in progress, exiting." >&2
    exit 0
fi

TODAY="$(date +%F)"
DIGEST_LOG="${LOG_DIR}/digest.log"

log() {
    echo "[$(date -Iseconds)] [stitch] $*" | tee -a "$DIGEST_LOG"
}

RUN_START_TS=$(date +%s)
log "=== stitch start: apply_links=${APPLY_LINKS} max_topics=${MAX_TOPICS} ==="

# --- Scope: notes touched in the last 24h. Raw archives, Filing Logs, and the
# usual excluded dirs are pre-filtered here so the agent never spends turns on
# them; daily/weekly notes are excluded by the prompt (their naming is a vault
# convention this script doesn't know).
(cd "$VAULT_DIR" && find . -name '*.md' -mtime -1 \
    -not -path './.*' \
    -not -path './Attachments/*' \
    -not -path './smart-chats/*' \
    -not -path './Raw Digests/*' \
    -not -path './Raw Email/*' \
    -not -path './Raw Chats/*' \
    -not -name 'Filing Log.md' \
    -printf '%TY-%Tm-%Td\t%P\n' | sort -r | head -n "$MAX_TOPICS") \
    > "${STAGING_DIR}/touched_notes.txt"

if [ ! -s "${STAGING_DIR}/touched_notes.txt" ]; then
    log "No notes touched in the last 24h - nothing to stitch."
    exit 0
fi
log "$(wc -l < "${STAGING_DIR}/touched_notes.txt") touched note(s) in scope"

# Complete note index (same shape the ingestion agents get: mtime<TAB>path).
(cd "$VAULT_DIR" && find . -name '*.md' \
    -not -path './.*' \
    -not -path './Attachments/*' \
    -not -path './smart-chats/*' \
    -printf '%TY-%Tm-%Td\t%P\n' | sort -k2) > "${STAGING_DIR}/vault_index.txt"

# Tails of every source's Filing Log (digest -> Raw Digests, personal -> Raw
# Email, telegram+whatsapp share Raw Chats).
{
    for d in "Raw Digests" "Raw Email" "Raw Chats"; do
        f="${VAULT_DIR}/${d}/Filing Log.md"
        if [ -f "$f" ]; then
            echo "## ${d}/Filing Log.md"
            tail -n 30 "$f"
            echo
        fi
    done
} > "${STAGING_DIR}/recent_filing_log.md"

# --- Pre-run snapshot (same incremental pattern as run-ingest.sh; shares the
# per-day dir). Needed in apply mode for the append-only post-check.
BACKUP_ROOT="${WORKDIR}/backups"
BACKUP_DIR="${BACKUP_ROOT}/${TODAY}"
PREV_BACKUP="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name '????-??-??' ! -name "$TODAY" 2>/dev/null | sort | tail -1)"
mkdir -p "$BACKUP_DIR"
if [ -n "$PREV_BACKUP" ]; then
    rsync -a --delete --link-dest="$PREV_BACKUP" "${VAULT_DIR}/" "${BACKUP_DIR}/"
else
    rsync -a --delete "${VAULT_DIR}/" "${BACKUP_DIR}/"
fi
log "Vault snapshot written to ${BACKUP_DIR}"

# Existence of proposed.md after the run proves the agent freshly wrote it.
rm -f "${STAGING_DIR}/proposed.md"

AGENT_LOG="${LOG_DIR}/agent.stitch.${TODAY}.json"
log "Invoking stitch agent (LLM_MODEL: ${LLM_MODEL:-unknown}, prompt: ${PROMPT_FILE}, log: ${AGENT_LOG})..."
set +e
python3 "${SCRIPT_DIR}/custom_agent_loop.py" "$PROMPT_FILE" 2>&1 | tee "$AGENT_LOG"
AGENT_RC=${PIPESTATUS[0]}
set -e
log "Agent finished with exit ${AGENT_RC} after $(($(date +%s) - RUN_START_TS))s"

if [ "$AGENT_RC" -ne 0 ]; then
    log "ERROR: stitch agent exited $AGENT_RC, see $AGENT_LOG"
    "$NOTIFY" "Stitch FAILED" "Stitch agent failed (exit $AGENT_RC). See $AGENT_LOG." high x
    exit 1
fi

if [ ! -s "${STAGING_DIR}/proposed.md" ]; then
    log "ERROR: agent exited 0 but proposed.md was not written"
    "$NOTIFY" "Stitch FAILED" "Stitch finished without writing its report. See $AGENT_LOG." high x
    exit 1
fi

# --- Append-only post-check (apply mode): every vault note modified during this
# run must be its snapshot copy plus appended bytes. Warn-only, like verify_filing.
VERIFY_MSG=""
if [ "$APPLY_LINKS" = "1" ]; then
    VERIFY_MSG="$(python3 - "$VAULT_DIR" "$BACKUP_DIR" "$RUN_START_TS" <<'PY'
import os, sys
vault, backup, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
bad = []
for root, dirs, files in os.walk(vault):
    dirs[:] = [d for d in dirs if not d.startswith(".")]
    for name in files:
        if not name.endswith(".md"):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < start:
                continue
        except OSError:
            continue
        rel = os.path.relpath(path, vault)
        old = os.path.join(backup, rel)
        if not os.path.exists(old):
            bad.append(f"CREATED (stitch must not create notes): {rel}")
            continue
        with open(old, "rb") as a, open(path, "rb") as b:
            if not b.read().startswith(a.read()):
                bad.append(f"MODIFIED IN PLACE (not append-only): {rel}")
if bad:
    print("append-only check FAILED:\n" + "\n".join("  - " + x for x in bad))
else:
    print("append-only check passed")
PY
)"
    log "$VERIFY_MSG"
    if [[ "$VERIFY_MSG" == *FAILED* ]]; then
        "$NOTIFY" "Stitch WARNING" "Stitch made a non-append change - review the ${TODAY} snapshot diff. ${VERIFY_MSG}" high warning
    fi
    # Live vault writes land root-owned; re-own for the PUID-1000 Obsidian
    # sync client (same fix as run-ingest.sh).
    chown -R 1000:1000 "$VAULT_DIR" 2>&1 | tee -a "$DIGEST_LOG" || true
fi

REPORT="$(python3 "${SCRIPT_DIR}/filing_report.py" "$AGENT_LOG" || true)"
log "Stitch complete (${REPORT:-no summary}). Report: ${STAGING_DIR}/proposed.md"
"$NOTIFY" "Stitch $([ "$APPLY_LINKS" = "1" ] && echo done || echo '(report-only) done')" \
    "${REPORT:-Stitch report ready.} Details: staging/stitch/proposed.md" default white_check_mark

log "=== stitch complete: total $(($(date +%s) - RUN_START_TS))s ==="
