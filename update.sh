#!/usr/bin/env bash
# Pulls the latest pipeline code, rebuilds the container, restarts it, and verifies
# nothing regressed (config drift, vault path). Safe to re-run anytime.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

BRANCH="${UPDATE_BRANCH:-main}"

echo "==> Checking working tree"
if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: local changes present. Commit, stash, or discard them before updating:" >&2
    git status --short >&2
    exit 1
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
