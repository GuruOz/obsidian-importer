#!/usr/bin/env bash
# Pulls the latest pipeline code, rebuilds the container, restarts it, and verifies
# nothing regressed (config drift, vault path). Safe to re-run anytime.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

BRANCH="${UPDATE_BRANCH:-main}"

# Kept in sync with EDITABLE_FILES in scripts/vault_web.py (minus .env, which is
# gitignored so it never shows up as a working-tree change). The dashboard's
# Settings tab writes straight to these host paths (docker-compose.yml mounts
# ./:/host into vault-qa) so its edits apply without an image rebuild - that's
# by design, so a live schedule/prompt tweak must not permanently block updates.
DASHBOARD_FILES=(
    crontab
    prompt_template.txt
    prompt_dry_run.txt
    prompt_vault_profile.txt
    prompt_weekly_rollup.txt
    prompt_personal_email.txt
    prompt_personal_email_dry_run.txt
    prompt_telegram.txt
    prompt_telegram_dry_run.txt
    prompt_whatsapp.txt
    prompt_whatsapp_dry_run.txt
    prompt_stitch.txt
    prompt_stitch_dry_run.txt
)

echo "==> Checking working tree"
UNEXPECTED="$(git status --porcelain -- . $(printf ':(exclude)%s ' "${DASHBOARD_FILES[@]}"))"
if [ -n "$UNEXPECTED" ]; then
    echo "ERROR: local changes present. Commit, stash, or discard them before updating:" >&2
    echo "$UNEXPECTED" >&2
    exit 1
fi

STASHED=0
DASHBOARD_DIRTY="$(git status --porcelain -- "${DASHBOARD_FILES[@]}")"
if [ -n "$DASHBOARD_DIRTY" ]; then
    echo "==> Stashing dashboard-edited config so it survives the pull:"
    echo "$DASHBOARD_DIRTY"
    git stash push --quiet --message "update.sh: dashboard config" -- "${DASHBOARD_FILES[@]}"
    STASHED=1
fi

CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo "WARNING: on branch '$CURRENT_BRANCH', expected '$BRANCH'. Pulling '$CURRENT_BRANCH' as-is."
fi

OLD_HEAD="$(git rev-parse HEAD)"

echo "==> Fetching and fast-forwarding"
git fetch origin
git pull --ff-only

NEW_HEAD="$(git rev-parse HEAD)"

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    echo "==> Already up to date ($NEW_HEAD)"
else
    echo "==> Pulled $(git rev-list --count "$OLD_HEAD..$NEW_HEAD") commit(s):"
    git log --oneline "$OLD_HEAD..$NEW_HEAD"
fi

if [ "$STASHED" = "1" ]; then
    echo "==> Restoring dashboard-edited config"
    if ! git stash pop --quiet; then
        echo "ERROR: restoring your dashboard config edits conflicted with what was pulled." >&2
        echo "The edits are still in the stash (git stash list). Resolve manually:" >&2
        echo "  git status && git diff" >&2
        echo "then 'git stash drop' once you're done, and re-run this script." >&2
        exit 1
    fi
fi

echo "==> Checking .env against .env.example"
if [ -f .env ] && [ -f .env.example ]; then
    EXAMPLE_VARS="$(grep -oE '^[A-Z_]+=' .env.example | tr -d '=' | sort -u)"
    ENV_VARS="$(grep -oE '^[A-Z_]+=' .env | tr -d '=' | sort -u)"
    MISSING="$(comm -23 <(echo "$EXAMPLE_VARS") <(echo "$ENV_VARS"))"
    STALE="$(comm -13 <(echo "$EXAMPLE_VARS") <(echo "$ENV_VARS"))"
    if [ -n "$MISSING" ]; then
        echo "WARNING: .env is missing variables present in .env.example:"
        echo "$MISSING" | sed 's/^/    /'
    fi
    if [ -n "$STALE" ]; then
        echo "NOTE: .env has variables no longer in .env.example (safe to remove if unused):"
        echo "$STALE" | sed 's/^/    /'
    fi
else
    echo "WARNING: .env or .env.example not found, skipping drift check."
fi

echo "==> Rebuilding locally-built images (pipeline, whatsapp-bridge)"
docker compose build

echo "==> Restarting containers"
docker compose up -d

echo "==> Verifying container"
docker compose exec -T pipeline python3 --version

echo "==> Verifying vault path"
docker compose exec -T pipeline sh -c '
    if [ -d "$VAULT_DIR" ]; then
        COUNT=$(find "$VAULT_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l)
        TOTAL=$(find "$VAULT_DIR" -name "*.md" 2>/dev/null | wc -l)
        echo "VAULT_DIR=$VAULT_DIR (exists, $COUNT notes at top level, $TOTAL total)"
        if [ -f "$VAULT_DIR/Filing_Rules.md" ]; then
            echo "Filing_Rules.md: found"
        else
            echo "Filing_Rules.md: NOT FOUND (run scripts/profile_vault.py once vault is populated)"
        fi
    else
        echo "ERROR: VAULT_DIR (\"$VAULT_DIR\") does not exist inside the container!" >&2
        exit 1
    fi
'

echo "==> Update complete."
