#!/usr/bin/env bash
# Re-export env vars from the live .env (project root, bind-mounted read-only at
# /hostro) so cron jobs and dashboard-exec'd runs pick up settings-page edits
# without a container recreate: the container's creation-time env (env_file:)
# only updates on `docker compose up -d`, which the dashboard cannot issue.
# Sourced by run-ingest.sh / run-stitch.sh / run-weekly.sh BEFORE any
# configuration is resolved. Safe under set -euo pipefail.
#
# Parsing semantics (values are LITERAL - never source/eval, values may hold
# spaces, $, or quotes):
#   * each line splits on the FIRST '='; everything after it is the value
#     verbatim, except one matching pair of surrounding quotes ("..." or '...')
#     is stripped to match Compose v2's dotenv parser
#   * blank lines and lines starting with '#' are skipped
#   * a trailing CR is stripped (Windows-edited file)
#   * key must match ^[A-Za-z_][A-Za-z0-9_]*$ or the line is skipped
#   * KEY= (empty value) exports an empty string - every consumer resolves
#     ${VAR:-default}, so empty means "use the default", same as creation env
#   * keys named in $INGEST_ENV_OVERRIDES (space-separated) keep their current
#     values: explicit per-run overrides beat the file, the file beats stale
#     creation-time env. CLI one-offs that override env must now say so:
#       docker compose exec -e WHATSAPP_DRY_RUN=0 \
#         -e INGEST_ENV_OVERRIDES=WHATSAPP_DRY_RUN pipeline \
#         /app/scripts/run-ingest.sh whatsapp

refresh_env_from_file() {
    local file="${1:-/hostro/.env}"
    if [ ! -f "$file" ]; then
        echo "[env-refresh] ${file} not found; using container creation env as-is"
        return 0
    fi
    local -a keep_keys=() keep_vals=()
    local k
    for k in ${INGEST_ENV_OVERRIDES:-}; do            # word-splitting intended
        [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [ -n "${!k+x}" ]; then                     # skip keys listed but unset
            keep_keys+=("$k"); keep_vals+=("${!k}")
        fi
    done
    local line key val count=0
    while IFS= read -r line || [ -n "$line" ]; do     # || catches a last line with no \n
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"       # trim leading whitespace
        [ -z "$line" ] && continue
        [[ "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # Never trusted from the file: a persisted value would permanently pin
        # stale creation-env values on every cron run.
        [ "$key" = "INGEST_ENV_OVERRIDES" ] && continue
        if [[ "$val" == \"*\" && "${#val}" -ge 2 ]]; then
            val="${val:1:${#val}-2}"
        elif [[ "$val" == \'*\' && "${#val}" -ge 2 ]]; then
            val="${val:1:${#val}-2}"
        fi
        # Probe in a subshell first: assigning to a readonly variable (e.g.
        # UID under bash) is a FATAL shell error that no if-guard catches -
        # let the throwaway child die instead of the run.
        if ! (export "$key=$val") 2>/dev/null; then
            echo "[env-refresh] skipped read-only variable ${key}" >&2
            continue
        fi
        export "$key=$val"
        count=$((count + 1))
    done < "$file"
    local i
    for i in "${!keep_keys[@]}"; do
        export "${keep_keys[$i]}=${keep_vals[$i]}"
    done
    echo "[env-refresh] re-exported ${count} setting(s) from ${file}${keep_keys[*]:+; kept per-run overrides: ${keep_keys[*]}}"
}
