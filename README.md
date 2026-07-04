# Copilot Digest → Obsidian Vault Pipeline

Nightly pipeline: pulls the M365 Copilot work-summary email from a personal Outlook/
Hotmail inbox via Microsoft Graph, stages it as markdown, and invokes an autonomous
tool-calling agent (`scripts/custom_agent_loop.py`, works with any OpenAI-compatible
API such as DeepSeek or OpenRouter) to split it into atomic entries and file them into
an Obsidian vault.

## Architecture

Two deployment modes, chosen by which service(s) you start:

- **This machine (current setup): native Obsidian already installed.** The `pipeline`
  service bind-mounts your real vault folder directly
  (`C:/Users/Guru/Documents/Galaxy Brain:/vault`). Your existing desktop Obsidian app
  already watches that folder and handles Sync - no extra container needed. Start with
  `docker compose up -d pipeline`.
- **Headless server, no desktop GUI.** The `obsidian` service (`linuxserver/obsidian`,
  a browser GUI at `http://localhost:3000` for one-time Sync login) runs instead,
  sharing a named volume with `pipeline`. It's gated behind the `headless-server`
  compose profile so it never starts by accident on a machine that doesn't need it:
  `docker compose --profile headless-server up -d`.

`pipeline` runs `supercronic` continuously, firing `scripts/run-digest.sh` at 21:30
Asia/Singapore daily. Pipeline state (staging, logs, backups, ledger, MSAL token
cache) lives in `./data`, bind-mounted so you can inspect it from
the host.

## One-time setup

### 1. Register an Entra app for Microsoft Graph (email access)

Personal Outlook/Hotmail accounts no longer support IMAP app passwords (Microsoft
retired Basic Auth for consumer mailboxes in Sept 2024), so this pipeline reads mail
via Microsoft Graph instead.

1. Go to https://portal.azure.com → **Microsoft Entra ID** → **App registrations** →
   **New registration**.
2. Name it anything (e.g. `copilot-digest-pipeline`).
3. Supported account types: **Personal Microsoft accounts only**.
4. After creation: **Authentication** → **Advanced settings** → set **"Allow public
   client flows"** to **Yes** → Save.
5. **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated
   permissions** → `Mail.Read` → **Add permissions**. (Read-only is enough: the
   pipeline never modifies your mailbox.)
6. Copy the **Application (client) ID** from the Overview page.

Then set up an **Outlook.com rule** (Settings → Mail → Rules) that moves incoming
digest emails into a dedicated folder, and point `DIGEST_FOLDER` in `.env` at it —
e.g. a `CopilotDigest` folder created inside the Inbox is `Inbox/CopilotDigest`. The
pipeline reads from that folder; already-processed emails are skipped via the ledger,
so nothing needs to be moved or deleted by the app.

### 2. Configure `.env`

```
cp .env.example .env
```

Fill in `GRAPH_CLIENT_ID` (from step 1), `DIGEST_FROM`, `DIGEST_SUBJECT_PATTERN`,
`NTFY_TOPIC` (any secret ntfy.sh topic name — subscribe to it in the ntfy phone app),
and the agent endpoint (see step 4). Leave `DRY_RUN=1` until milestone M5.

### 3. Start the pipeline

```
docker compose up -d pipeline
```

(On a headless server with no native Obsidian install, use
`docker compose --profile headless-server up -d` instead, then open
http://localhost:3000 to log into Obsidian Sync once - see the Architecture section.)

### 4. Configure the agent endpoint

Set the three `LLM_*` variables in `.env` to any OpenAI-compatible chat-completions
endpoint with tool-calling support, e.g.:

```
# DeepSeek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat

# or OpenRouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=<provider/model>
```

Then `docker compose up -d` again to pick the changes up.

### 5. Authenticate Microsoft Graph

```
docker compose run --rm pipeline python3 scripts/graph_auth.py
```

Follow the printed device-code URL to sign in once. The refresh token is cached to
`./data/msal_cache.json` and silently renewed on every future run.

## Build order / milestones

1. **M0 — Docker skeleton.** Step 3 above. Verify the pipeline container can see your
   real vault files: `docker compose exec pipeline ls /vault`.
2. **M1 — Ingestion.** Steps 1, 2, 5 above, then test manually:
   `docker compose exec pipeline python3 scripts/fetch_digest.py`. Confirm
   `data/staging/digest.md` looks right and the ledger (`data/processed_ids.txt`)
   updated. Also confirm the "no digest" path exits 20 on a second run.
3. **M2 — Conventions.** Run the **vault profiler** to generate `Filing_Rules.md` at
   the vault root (see below), and hand-refine it.
4. **M3 — Agent dry-run.** With `DRY_RUN=1` (default), run
   `docker compose exec pipeline scripts/run-digest.sh` and review
   `data/staging/proposed.md` against real digests for a couple of weeks.
5. **M4 — Orchestration + safety.** Confirm supercronic fires nightly
   (`docker compose logs -f pipeline` around 21:30 SGT), snapshots appear under
   `data/backups/YYYY-MM-DD/`, and ntfy notifications arrive.
6. **M5 — Go live.** Set `DRY_RUN=0` in `.env`, `docker compose up -d` to restart the
   pipeline container with the new setting.

## Vault Profiler (run once, milestone M2)

`scripts/profile_vault.py` traverses the vault, maps the folder tree, takes a
stratified random sample of markdown files (a few per folder, capped in total), and
stages that profile. It then invokes the agent loop (same `LLM_*` endpoint as the
nightly filing step) to synthesize `Filing_Rules.md` at the vault root: the folder
taxonomy, daily-note format, tag vocabulary, linking style, and create-vs-update rule,
inferred from what's actually in the vault. The durable process rules (append-only
discipline, idempotency marker) live in the nightly prompt templates;
`Filing_Rules.md` holds the regenerable structural facts and is injected into the
agent's system prompt on every run.

```
# Just inspect the sampled profile first, no LLM call, no cost:
docker compose run --rm pipeline python3 scripts/profile_vault.py --profile-only

# Generate Filing_Rules.md for real:
docker compose run --rm pipeline python3 scripts/profile_vault.py

# Regenerate later after reorganizing the vault:
docker compose run --rm pipeline python3 scripts/profile_vault.py --force
```

Tuning flags: `--samples-per-folder` (default 3), `--max-samples` (default 60),
`--max-chars-per-file` (default 800) — raise these for a more thorough profile at the
cost of a larger prompt (and more token usage) on that one run.

## Ask your vault

Read-only Q&A over the vault, using the same agent loop and LLM endpoint but with
write/edit tools removed entirely:

```
./ask.sh "what did I do on ticket CS0012345?"
./ask.sh "summarize my Purview work this month"
```

The agent gets the full note index up front, reads the relevant notes, and answers
with the source note paths listed. Nothing is ever written.

## Weekly reviews

Every Sunday at 20:00 (before the nightly digest run) the pipeline synthesizes the
week's daily notes into a single "Weekly Review YYYY-Wnn" note: highlights, recurring
themes, and open loops, all wikilinked back to the source notes. Placement follows
`Filing_Rules.md` conventions (falling back to a `Weekly/` folder at the vault root).
It is create-only — an existing review note for the week is never overwritten — and
it skips entirely while `DRY_RUN=1`. Run one on demand with:

```
docker compose exec pipeline scripts/run-weekly.sh
```

## Operational notes

- **Updating:** run `./update.sh`. It pulls the latest code (fast-forward only — refuses
  to run with local changes), reports exactly which commits came in, flags any drift
  between `.env` and `.env.example` (missing new keys, stale dead ones), rebuilds and
  restarts the `pipeline` container, then verifies the new image took and that
  `VAULT_DIR` still resolves to a populated vault with `Filing_Rules.md` present.
- **Windows hosts:** enable "Start Docker Desktop when you sign in" in Docker Desktop
  settings, so the always-on guarantee survives reboots.
- **Re-authenticating Graph:** if `fetch_digest.py` exits with code 30 (and you get a
  "Graph auth expired" ntfy alert), re-run step 5.
- **Recovering from a bad run:** restore the relevant `data/backups/YYYY-MM-DD/`
  snapshot by copying it back into the `vault` volume
  (`docker compose exec pipeline rsync -a --delete /work/backups/YYYY-MM-DD/ /vault/`).
- **Manual run:** `docker compose exec pipeline scripts/run-digest.sh` (or
  `docker compose run --rm pipeline scripts/run-digest.sh` if the pipeline container
  isn't already up).
- **Audit trail:** every run that changes the vault appends a wikilinked line to
  `Raw Digests/Filing Log.md` (status, work date, entry count, every note touched),
  and the ntfy notification carries the same summary — so the phone alert tells you
  *what* was filed, not just that something was.
- **Idempotency:** two independent layers — the `internetMessageId` ledger
  (`data/processed_ids.txt`) prevents re-ingesting the same email, and the
  `<!-- copilot-digest:YYYY-MM-DD -->` marker in each daily note prevents the agent
  from re-filing the same day twice, even on a manual re-run. The ledger is committed
  only after the agent step succeeds, so a night that fails mid-run (API outage, bad
  key) is retried automatically the next night instead of being lost.
- **Mailbox is never modified:** the Graph app has `Mail.Read` only. Inbox tidiness
  comes from your Outlook.com rule moving digests into the `DIGEST_FOLDER`.
- **Cost controls:** the nightly run's cost scales with the digest's entry count.
  The levers that keep it down: `run-digest.sh` pre-builds `staging/vault_index.txt`
  (a complete note-path list) so the agent shortlists create-vs-update candidates by
  title instead of searching the vault turn by turn; the prompts demand concise
  output; the agent loop hard-stops after 30 tool-calling iterations; and `LLM_MODEL`
  lets you pick a cheap model (e.g. DeepSeek) in the first place.
- **Backups are incremental:** each day's snapshot hard-links unchanged files against
  the previous day's (rsync `--link-dest`), so 14 retained days cost roughly one full
  copy plus deltas. On filesystems without hard-link support it silently degrades to
  full copies.
