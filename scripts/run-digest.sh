#!/usr/bin/env bash
# Backwards-compatible entry point for the nightly M365 Copilot digest.
#
# The digest is now just one source of the generic ingestion framework in
# run-ingest.sh; this wrapper is kept so the existing cron line, the docs, and
# manual invocations all keep working exactly as before:
#
#   scripts/run-digest.sh                # tonight's digest
#   scripts/run-digest.sh 2026-07-03     # backfill a specific work date
#
# All of the digest's paths (staging, ledger, Raw Digests archive, DRY_RUN) and
# behaviour live in run-ingest.sh under the `digest` source.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/run-ingest.sh" digest "$@"
