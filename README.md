# Copilot Digest → Obsidian Vault Pipeline

Nightly pipeline: pulls the M365 Copilot work-summary email from a personal Outlook/
Hotmail inbox via Microsoft Graph, stages it as markdown, and invokes an autonomous
tool-calling agent (`scripts/custom_agent_loop.py`, works with any OpenAI-compatible
API such as DeepSeek or OpenRouter) to split it into atomic entries and file them into
an Obsidian vault.

## Architecture

Three services, one `docker compose up -d` starts all of them (no profiles - every
service in `docker-compose.yml` runs by default):

- **`obsidian`** (`linuxserver/obsidian`): a browser GUI at `http://localhost:3000` for
  one-time Obsidian Sync login. It owns the vault - a named Docker volume
  (`obsidian_only_vault`), isolated from the host filesystem.
- **`pipeline`**: runs `supercronic` continuously, firing `scripts/run-digest.sh` at
  21:30 Asia/Singapore daily to file the nightly digest into that same vault volume,
  and `scripts/run-ingest.sh personal` at 22:30 to triage and file your personal inbox
  (see "Personal inbox triage" below). Pipeline state (staging, logs, backups, ledgers,
  MSAL token cache) lives in `./data`, bind-mounted so you can inspect it from the host.
- **`vault-qa`**: the persistent chat web UI over the vault - see "Chat with your
  vault" below. Read-only; mounts the vault volume `:ro`.

All three share the `obsidian_only_vault` volume and `restart: unless-stopped`, so a
single `docker compose up -d` (step 3 below) is a one-time action - Docker keeps them
running across crashes and host reboots from then on, and `update.sh` re-runs the same
bare `up -d` on every future update.

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

### 3. Start everything

```
docker compose up -d
```

Starts all three services (see Architecture above). Open `http://localhost:3000` to
log into Obsidian Sync once. This is the same command `update.sh` runs on every
future update, so there's nothing further to remember here.

### 4. Configure the agent endpoint

Set the three `LLM_*` variables in `.env` to any OpenAI-compatible chat-completions
endpoint with tool-calling support, e.g.:

```
# DeepSeek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-v4-flash

# or OpenRouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=<provider/model>
```

> DeepSeek retired the `deepseek-chat` and `deepseek-reasoner` model names on
> 2026-07-24; they now return HTTP 400. Use `deepseek-v4-flash` (or the stronger,
> pricier `deepseek-v4-pro`). The agent logs a warning at start-up if it sees a
> retired name.

Then `docker compose up -d` again to pick the changes up. (Pipeline runs re-read
the live `.env` at run start, so most later edits apply on the next run without
this — but the chat assistant's `LLM_*`/`VAULT_QA_*` settings and the WhatsApp
bridge read theirs at container creation and do need the `up -d`.)

**Thinking mode.** `LLM_THINKING=1` (the default) lets the model reason before it
answers; `LLM_REASONING_EFFORT` picks how hard, on a `minimal | low | medium |
high | max` ladder that is normalized per provider — DeepSeek only really
distinguishes `high` from `max`, OpenAI stops at `high`, Ollama accepts
`low/medium/high`, and an endpoint that has never heard of the parameter has it
stripped automatically rather than failing the run. Both are settable from the
dashboard's **AI Model** settings, and per source via the `PERSONAL_MAIL_` /
`TELEGRAM_` / `WHATSAPP_` prefixes. Reasoning tokens are billed as **output**, so
this is the main dial on what a nightly run costs. The chat assistant has its own
effort picker next to the message box, so you can spend more on a hard question
without changing anything global.

**Prompt caching.** Nothing to configure — every provider here caches on matching
request *prefixes*, and the prompts are laid out to give them a long stable one
(instructions and vault index first, the changing date/time last). On DeepSeek a
cached input token costs about 2% of an uncached one. The chat shows the hit rate
in each answer's footer (`↑12400 (91% cached)`), and the pipeline logs it per turn.

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
6. **M5 — Go live.** Set `DRY_RUN=0` in `.env` (or flip **Dry run** off on the
   dashboard's Work Digest settings card) — the next run picks it up automatically;
   no restart needed. Note this switch governs only the work digest: personal mail,
   Telegram, and WhatsApp each have their own `*_DRY_RUN` switch.

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
with the source note paths listed. Nothing is ever written. It also has a lightweight
BM25 lexical-search tool (`search_relevant`) for thematic/paraphrased questions where
`grep_search`'s exact matching won't surface the right notes.

## Chat with your vault

A persistent, NotebookLM-style chat UI over the same read-only Q&A agent — multi-turn
conversation, live "searching for X..." progress while the agent works, and answers
with clickable source citations (`obsidian://open?...` links — open directly in the
Obsidian app on a phone or desktop with the URI scheme registered; inert text
elsewhere). Runs as its own always-on service:

```
docker compose up -d vault-qa
```

Then open `http://<host>:8420/` in a browser. Like the Obsidian GUI at :3000, this is
**LAN-only with no authentication** — anyone on your network can query the vault and
spend LLM tokens doing it. Add a shared-secret check in `vault_web.py`'s
`_check_auth()` hook if that's a concern on your network.

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

## Personal inbox triage (second source)

The nightly digest is one *ingestion source* of a small framework
(`scripts/run-ingest.sh <source>`); the digest's own entry point,
`scripts/run-digest.sh`, is now a thin wrapper for `run-ingest.sh digest`, so the
cron line, ledger, and `Raw Digests/` archive are unchanged.

A second source, `personal`, reads your **whole Inbox** (same read-only
`Mail.Read` token — no new consent), lets the agent **triage** each new email, and
files the ones worth keeping:

- **Starts from now.** The first run just records a watermark set to the current
  time and files nothing — nothing before today is ever ingested. Every run after
  files Inbox emails received since the watermark (oldest-first, capped at
  `PERSONAL_MAIL_MAX_PER_RUN`, default 25, so a flood drains across nights).
- **Triage.** The agent skips marketing, newsletters, OTPs, automated
  notifications, and routine receipts; it files personal correspondence, plans and
  bookings, finance/health/legal events, and commitments/decisions. Every skip is
  logged with a one-line reason to both the ntfy alert and `Raw Email/Filing Log.md`
  — you audit the judgment, you don't trust it blindly.
- **Dry-run by default, independently of the digest.** `PERSONAL_MAIL_DRY_RUN`
  defaults to `1` and does **not** follow the global `DRY_RUN`, so personal mail can
  propose to `data/staging/personal/proposed.md` for a week while the digest keeps
  filing live. Review the proposals, then set `PERSONAL_MAIL_DRY_RUN=0` (the
  dashboard's Personal Email settings card has the toggle; it applies from the
  next run — no restart needed).
- **Same safety nets as the digest:** shared vault lock (the two sources never
  touch the vault at once), pre-run snapshot, its own ledger
  (`data/personal_processed_ids.txt`) and watermark
  (`data/personal_watermark.txt`) that advance only after a successful run, a
  per-email `<!-- personal-email:<Message-ID> -->` idempotency marker, and a raw
  archive to `Raw Email/Personal Mail - <date>.md`.

It runs nightly at 22:30 (an hour after the digest). Optional overrides —
including pointing it at a different model via
`PERSONAL_MAIL_LLM_BASE_URL`/`PERSONAL_MAIL_LLM_MODEL`, e.g. a **local Ollama
server** (`http://host.docker.internal:11434/v1`, no API key needed — see
`.env.example`) — are documented in `.env.example`. Run one on demand with:

```
docker compose exec pipeline scripts/run-ingest.sh personal
```

Or from the web UI's **Settings → Personal Email Ingestion** panel, which can
also **stop** a run mid-flight (safe: nothing is marked processed until a pass
completes, so aborted emails are re-fetched next time), backfill with either a
lookback or an explicit **start/end date window** (the window bypasses the
nightly watermark and leaves it untouched), and dry-run without side effects —
dry runs never touch the ledger or watermark, so a later live run sees the
same mail again.

> Whole-inbox mode sends every new email body (capped) to your configured LLM
> endpoint — broader exposure than the digest-only flow. The per-source model
> override above is the escape hatch if you'd rather keep personal mail on a
> different provider.

**Adding a future source** (calendar, bookmarks, bank-statement CSVs) is just: a
fetcher that stages markdown + `pending_ids.txt`, a live+dry-run prompt pair, a
`case` entry in `run-ingest.sh`, and — if it's interactive — a `.env` block and a
Connections page. The Telegram and WhatsApp sources below are worked examples.

## Chat sources: Telegram & WhatsApp

Two more sources file your **personal chats** into the vault the same way the
email sources do — full LLM triage-and-file, one **chat-day section** per
conversation per Singapore day, filed under that day's daily note with a
`<!-- telegram:<chat>:<date> -->` / `<!-- whatsapp:<chat>:<date> -->` idempotency
marker. Both support a one-time **historical import** and nightly capture of the
day's messages, with **include/exclude** lists so you choose which chats/groups
are filed. They run nightly at **23:00 (Telegram)** and **23:30 (WhatsApp)**,
after the email sources, sharing the same vault lock.

> ⚠️ **WhatsApp caveat — read this.** The WhatsApp bridge uses **Baileys**, an
> unofficial WhatsApp Web client. Using it technically violates WhatsApp's Terms
> of Service and carries a small but real risk of your number being banned.
> Passive personal archiving from a linked device is low-profile and the bridge
> **never sends messages**, but the risk isn't zero — proceed only if you accept
> it. Telegram's use (Telethon) is a normal user-account API session and does not
> carry the same standing risk.

**How they differ under the hood.** Telegram history is fully server-side, so
`fetch_telegram.py` is a plain nightly fetcher (Telethon/MTProto — the Bot API
can't read your own chats). WhatsApp requires a **long-lived linked-device
socket**, so an always-on `whatsapp-bridge` container (Baileys, Node) maintains
the session and appends messages to `data/whatsapp/messages/*.jsonl`; the nightly
`fetch_whatsapp.py` just reads that store (no network).

### One-time setup

1. **Pull & build on the server:** `git pull && docker compose build && docker
   compose up -d`. This builds the pipeline image (now with `tzdata`) **and** the
   `whatsapp-bridge` image, and restarts supercronic so the new crontab and the
   corrected Singapore-time schedule take effect.
2. **Telegram credentials:** create an app at <https://my.telegram.org> → *API
   development tools*, then put the **API ID** and **API hash** into **Settings →
   Telegram** and Save.
3. **Log in / pair (dashboard → Connections):**
   - *Telegram:* enter your phone → the code Telegram sends you → your two-step
     password if you have one. (Terminal fallback: `docker compose exec -it
     pipeline python3 scripts/telegram_login.py`.)
   - *WhatsApp:* on your phone, WhatsApp → **Linked Devices → Link a Device**, and
     scan the QR shown on the page. **Keep your phone online** until the initial
     history sync finishes.
4. **Pick chats:** the Connections page lists every discovered chat. Copy the
   exact names (or IDs/JIDs) into the **Include/Exclude** fields on the Telegram
   and WhatsApp settings cards. A non-empty *Include* list is an allowlist;
   otherwise everything is filed except *Exclude* (with the group/channel/bot
   toggles applying to Telegram).
5. **Dry-run first, then backfill, then go live:** from **Settings → Run
   Ingestion**, pick the source, keep **Dry run** on, and run a small window
   (e.g. yesterday). Review `data/staging/<source>/proposed.md`. Then backfill
   history with a **start/end date window** (dry-run first; for Telegram, set
   `TELEGRAM_START_DATE=all` for the full history). When you're happy, set
   `TELEGRAM_DRY_RUN=0` / `WHATSAPP_DRY_RUN=0` for nightly live filing — the
   **per-source** switches (on the Telegram/WhatsApp settings cards): the global
   `DRY_RUN` governs only the work digest and never takes chats live. Saving
   applies from the next run; no restart needed.
6. **Optional — save quota:** chat volume is the main quota driver. Point
   `TELEGRAM_LLM_*` / `WHATSAPP_LLM_*` at a cheaper or local (Ollama) model to
   keep nightly filing cheap.

If the WhatsApp session drops (WhatsApp evicts idle linked devices after ~14
days offline), the bridge reports `logged_out`, sends an ntfy alert, and the
fetcher exits 30 — just **Re-pair** from the Connections page.

## Operational notes

- **Settings apply without a restart:** the entry scripts (`run-ingest.sh`,
  `run-stitch.sh`, `run-weekly.sh`) re-read the live `.env` at run start
  (`scripts/env_refresh.sh`, via the read-only `/hostro` project mount), so
  dashboard saves and hand edits reach the very next run — nightly or manual —
  without touching the container. Exceptions: the crontab needs a pipeline
  restart (supercronic reads it once at startup; the dashboard offers this),
  and always-on services (vault-qa chat, whatsapp-bridge) read their env at
  container creation, so their settings need `docker compose up -d` on the
  host. Deploying this mechanism itself requires one
  `git pull && docker compose build && docker compose up -d` (a recreate — a
  plain `docker compose restart` never applies `.env` changes).
  CLI one-offs that override env now must name their overrides or the refresh
  wins for keys present in `.env`:
  `docker compose exec -e WHATSAPP_DRY_RUN=1 -e INGEST_ENV_OVERRIDES=WHATSAPP_DRY_RUN pipeline /app/scripts/run-ingest.sh whatsapp`.
- **Updating:** run `./update.sh`. It pulls the latest code (fast-forward only — refuses
  to run with local changes), reports exactly which commits came in, flags any drift
  between `.env` and `.env.example` (missing new keys, stale dead ones), rebuilds and
  restarts the `pipeline` container, then verifies the new image took and that
  `VAULT_DIR` still resolves to a populated vault with `Filing_Rules.md` present.
- **Windows hosts:** enable "Start Docker Desktop when you sign in" in Docker Desktop
  settings, so the always-on guarantee survives reboots.
- **Timezone (Singapore):** all timestamps, logs, filing dates, and cron schedules
  run in `Asia/Singapore` (`TZ` in `docker-compose.yml`; the image now ships
  `tzdata`, and Python routes local-time calls through `scripts/tzutil.py`). Graph
  filters and the source watermark files stay in UTC internally, which is correct —
  only displayed times and calendar-day boundaries are localized. **On first deploy
  of this change**, cron jobs move from firing 8 hours late (the old `tzdata`-less
  UTC fallback) to their intended SGT times; the ledger prevents any double-filing
  across the shift.
- **Re-authenticating Graph:** if a fetcher exits with code 30 (and you get a
  "Graph auth expired" ntfy alert), re-run step 5. The same `Mail.Read` token serves
  both the digest and the personal-inbox source.
- **Recovering from a bad run:** restore the relevant `data/backups/YYYY-MM-DD/`
  snapshot by copying it back into the `vault` volume
  (`docker compose exec pipeline rsync -a --delete /work/backups/YYYY-MM-DD/ /vault/`).
- **Manual run:** `docker compose exec pipeline scripts/run-digest.sh` (or
  `docker compose run --rm pipeline scripts/run-digest.sh` if the pipeline container
  isn't already up).
- **Filing under a specific date:** the prompts are date-agnostic - the run injects
  today's date automatically, and you can force the work date from one place when
  backfilling: `docker compose exec pipeline scripts/run-digest.sh 2026-07-03`
  (or set `WORK_DATE=2026-07-03`). With an override the agent files under exactly
  that date instead of inferring one from the digest.
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
