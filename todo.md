# Historical Changelog & Completed Tasks

This document serves as a historical record of architectural decisions and fixes applied to the `obsidian-importer` project. LLM Agents should read this to understand *why* the codebase is structured the way it is, and to avoid reverting necessary constraints.

## Problem 1: Over-reliance on Native Anthropic CLI
*   **The Issue:** The initial pipeline solely relied on the native `claude-code` CLI. This lacked flexibility, prevented the usage of more cost-effective models (like DeepSeek V4 Pro or OpenRouter), and tied the deployment strictly to Anthropic's ecosystem.
*   **The Fix (Dual-Engine Architecture):** 
    *   Created `scripts/custom_agent_loop.py`, a robust, standalone Python agent loop powered by the standard `openai` SDK.
    *   Exposed identical tool schemas to this custom loop (`read_file`, `glob_search`, `grep_search`, `write_file`, `edit_file`). This ensures the custom agent can dynamically navigate and edit the local filesystem independently, exactly like Claude.
    *   Updated `scripts/run-digest.sh` and `scripts/profile_vault.py` to seamlessly toggle between the native `claude` CLI and the custom Python loop based on the `AGENT_ENGINE` environment variable.
    *   Added `.env` secrets for `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`.

## Problem 2: Tool Output Instability
*   **The Issue:** The custom agent was frequently outputting raw text at the end of its run instead of outputting the required structured JSON payload, breaking the downstream pipeline logging.
*   **The Fix:** Modified the agent system prompts (`prompt_template.txt` and `prompt_dry_run.txt`) to require the use of a dedicated `finish` tool. The custom agent loop intercepts this tool call and formats its arguments as the final structured JSON output.

## Problem 3: Poor Context & Unreliable File Lookups
*   **The Issue:** The agent was originally using expensive and sometimes failing `grep_search` calls just to discover what notes existed in the vault. This led to hallucinated paths or the agent getting lost.
*   **The Fix:** Updated the orchestrator `scripts/run-digest.sh` to pre-compute `vault_index.txt` (a flat list of all markdown files in the vault, ignoring attachments). This file is injected into the agent's context, giving it an immediate, reliable source of truth for all existing notes.

## Problem 4: Destructive Summarization
*   **The Issue:** The LLM was compressing and summarizing the raw digests, omitting critical operational details, technical parameters, and names of colleagues.
*   **The Fix (Lossless Rule):** Enforced a strict "Lossless Preservation" rule in the system prompts. The agent is explicitly forbidden from omitting details or summarizing context.

## Problem 5: The "Umbrella Note" Anti-Pattern
*   **The Issue:** The agent would frequently append distinct, highly specific troubleshooting scenarios into generic umbrella notes (e.g., throwing a new specific bug into a general `Purview policy tips limitation.md` file), making the vault incredibly cluttered and hard to navigate.
*   **The Fix (Aggressive Granularity):** Updated `prompt_template.txt` to explicitly ban this behavior. The agent MUST NOT append distinct scenarios to generic titles; instead, it MUST CREATE a new, highly specific note (e.g., `Alert triggering when cursor hovering on file.md`) for every distinct incident.

## Problem 6: Ambiguous Date Formatting
*   **The Issue:** The agent was using ambiguous dates, leading to confusion between DD/MM and MM/DD.
*   **The Fix:** Enforced the strict usage of the `YYYY-MM-DD (MMM)` format (e.g., `2026-07-03 (Jul)`) across all ingested notes, which also aids chronological sorting.

## Problem 7: Lack of Raw Traceability in Production
*   **The Issue:** After a successful production run, the raw `digest.md` email file was deleted or lost, making it impossible to audit what the agent processed.
*   **The Fix:** Integrated an archival step into `scripts/run-digest.sh`. On a successful run (`DRY_RUN=0`), the raw `digest.md` is automatically copied into `<Vault>/Raw Digests/Copilot Digest - <DATE>.md`.

## Problem 8: Docker Image Staleness
*   **The Issue:** The `run-digest.sh` updates were applied to the host filesystem, but the Docker container was still running the old logic because `scripts/` is `COPY`'d during the Docker image build rather than being a live bind-mount.
*   **The Fix:** Rebuilt the `obsidian-importer-pipeline` Docker image using `docker compose build pipeline` and restarted the container (`docker compose up -d pipeline`). *Note for future AI agents: Remember to rebuild the image if modifying the shell or python scripts!*

## Problem 9: Host Filesystem Pollution
*   **The Issue:** The pipeline was originally configured to bind directly to the host's native `Galaxy Brain` vault (`C:\Users\Guru\Documents\Galaxy Brain`). This violated the core requirement of total isolation.
*   **The Fix:** Removed the host bind mount in `docker-compose.yml`. The pipeline now mounts a Docker volume (`obsidian_only_vault`). The standalone Obsidian container (previously hidden behind a `headless-server` profile) was re-enabled as the primary UI to manage this isolated vault via VNC on port 3000.

## Problem 10: Dual-Engine Complexity
*   **The Issue:** After committing to DeepSeek via the custom loop, the dual-engine architecture from Problem 1 became dead weight: the `AGENT_ENGINE` toggle, the `CLAUDE_*` env vars, the npm-installed `claude-code` CLI in the image, and the vault-seed `CLAUDE.md` (whose only purpose was the Claude CLI's cwd auto-load — the custom loop never read it, so its rules were silently skipped on that path).
*   **The Fix (Single Engine):** Removed the `claude` CLI path entirely from `run-digest.sh`, `profile_vault.py`, `.env.example`, and the Dockerfile (base image is now `python:3.12-slim`, no Node). Deleted `vault-seed/CLAUDE.md`: its durable rules (append-only, idempotency marker) were already duplicated in the prompt templates, and `Filing_Rules.md` — still generated by the profiler — is now injected directly into the agent's system prompt on every run, restoring the auto-load guarantee the Claude CLI used to provide. `write_file` also exempts `Filing_Rules.md` from the DRY_RUN restriction so the profiler works before go-live.

## Problem 11: No Freshness Signal in Agentic Search
*   **The Issue:** `search_relevant` (BM25 over vault chunks) ranked purely by lexical relevance, so a years-old note could outrank a current one describing the same topic - exactly the staleness problem web search engines solve with freshness boosting.
*   **The Fix (Recency Weighting):** `lexical_index.py` now multiplies each chunk's BM25 score by a freshness factor derived from the note file's mtime: exponential decay halving every `VAULT_QA_RECENCY_HALFLIFE_DAYS` (default 90), floored at `VAULT_QA_RECENCY_FLOOR` (default 0.5) of the raw score so a strongly-more-relevant old note still wins - recency breaks ties, relevance stays primary. Each search hit now shows its age in days, and the tool description tells the agent about the weighting. Set `VAULT_QA_RECENCY_FLOOR=1` to disable.

## Problem 12: Thin Sampling for Filing_Rules.md on Larger Vaults
*   **The Issue:** `profile_vault.py --max-samples` defaulted to 60, inherited from early testing on a small vault. For a vault in the 800-2000 note range, 60 samples is well under 10% coverage, so the synthesized `Filing_Rules.md` (folder taxonomy, tag vocabulary, linking style) risks missing conventions used in folders that didn't get sampled.
*   **The Fix:** Raised the default to 200, chosen to roughly triple coverage while staying well inside typical LLM context budgets (200 files x the default 800-char `--max-chars-per-file` cap is ~160k characters, ~40k tokens - comfortable for the models this pipeline targets). Still a CLI flag (`--max-samples`), so it can be tuned up further for very large vaults or down if a smaller/context-limited model is swapped in via `LLM_MODEL`.

## Planned Backlog: Chat UI Overhaul (vault-qa)

A queued feature program for the `vault-qa` chat UI (`scripts/vault_web.py` +
`scripts/web/index.html`), aiming for a Claude/Gemini-grade experience over the
read-only vault Q&A backend. Implemented incrementally in batches of 1-2 features -
check items off as they land. Constraints that shape every batch: single self-hosted
HTML file, no CDN dependencies (LAN-only deployment), and the backend's answer
arrives via a `finish` tool call (not assistant text), which matters for streaming.

### 1. Core Chat & Messaging UI
- [ ] **Streaming responses** - token-by-token rendering of the final answer without
      layout shift. *Backend note: today the answer is the `finish` tool call's
      arguments, which cannot be streamed; needs a protocol change (final answer as
      streamed assistant text after tool use) before the UI part is possible.*
- [x] **Dynamic input box** - elastic textarea, grows to a max height; Enter submits,
      Shift+Enter inserts a newline. *(Batch 1)*
- [x] **Message controls** - copy-whole-message button on each AI response;
      "Regenerate Response" on the last AI turn (server rewinds to the last user
      turn via a `regenerate` flag on /api/chat - no duplicate user message).
      *(Batch 2; per-code-block copy buttons landed in Batch 1.)*
- [ ] **Inline stop control** - Send button morphs into Stop while streaming; abort
      solidifies the partial response. *Backend note: needs a server-side cancel path
      for the worker thread, not just closing the SSE stream.*
- [x] **Markdown & code rendering** - lists, bold/italic, tables, blockquotes,
      headings, links, inline code, fenced code blocks with lightweight syntax
      highlighting and per-block copy buttons. *(Batch 1. LaTeX deferred - requires
      vendoring KaTeX (~large) to stay CDN-free; revisit if math actually appears in
      vault answers.)*

### 2. Knowledge Base & Grounding
- [x] **Inline citations** - finish schema extended with `citations:
      [{path, snippet}]`; [n] markers in the answer become clickable sups (DOM
      post-pass over the rendered markdown, skipping code blocks) that toggle a
      panel with the note title (obsidian:// link) + quoted snippet. Persists /
      hydrates via the stored transcript; Markdown export lists them; ask_vault
      CLI prints them. *(Batch 7)*
- [x] **Active context indicator** - badge under the chat showing vault note count
      (from /api/meta on load) and per-session note + chunk counts (from the
      session SSE event). *(Batch 2)*
- [x] **Suggested prompts** - finish schema extended with `followups: [str]`;
      2-3 chips render under the newest answer only (older rows are removed,
      same pattern as Regenerate) and clicking one submits it as the next user
      message. *(Batch 7)*

### 3. Session & Sidebar Management
- [x] **Conversation history sidebar** - collapsible panel (☰, state persisted)
      listing sessions newest-first with auto-generated titles from the first
      message; server keeps a display-layer `transcript` per session, exposed via
      GET /api/sessions. *(Batch 3)*
- [x] **Session operations** - inline rename (✎ -> input, Enter/blur commits, Esc
      cancels; PATCH /api/sessions/<id>) and delete (🗑 + confirm; DELETE, 409 if a
      request is in flight; deleting the open chat falls back to a fresh one).
      *(Batch 3)*
- [x] **Chat view persistence** - switching sessions keeps rendered DOM, scroll
      position, draft text, and pending-retry state in a client view cache;
      uncached sessions (page reload) re-hydrate from GET /api/sessions/<id>,
      which replays the transcript incl. sources/usage/elapsed. *(Batch 3)*
- [x] **Durable chat storage** - sessions are persisted to SQLite (stdlib,
      `session_store.py`, default `/data/sessions.db` - a `./data/vault-qa`
      bind mount since the vault volume is read-only for this container).
      Server memory is now only a live-session cache: every mutation is
      written through, `VAULT_QA_MAX_SESSIONS` eviction and container
      restarts lose nothing, and continuing an old chat revives it by
      replaying the stored transcript into fresh model history (tool-call
      chatter from before the restart is intentionally dropped). *(Batch 4)*

### 4. Performance & Token Telemetry
- [ ] **Extended reasoning toggle** - render model reasoning in a collapsible
      `<details>` block above the answer, when the backend model exposes it.
- [x] **Token & performance counters** - per-response input/output tokens and wall
      time rendered under each answer. run_loop accumulates `usage` across all API
      calls via an optional `usage_out` dict (CLI callers unaffected). *(Batch 2)*

### 5. Conversation Forking & Branching
- [ ] **Message editing** - edit icon on past user messages turning them into an
      input box.
- [ ] **Branch navigation** - editing forks the timeline; < 2/3 > pagination steps
      between forks non-destructively. *Significant server-side session-model change
      (message tree instead of flat list).*

### 6. Quality of Life & Keyboard Ergonomics
- [ ] **Keyboard shortcuts** - *partial:* Cmd/Ctrl+K (or O) starts a new chat, and
      there's a header "New chat" button *(Batch 2)*. Still pending: Up-arrow edits
      last message (needs message editing, §5) and Esc stop (needs stop control, §1).
- [x] **Chat export** - header "Export" menu downloads the stored transcript as
      Markdown (You/Assistant sections + sources) or JSON (full payloads incl.
      usage/elapsed/feedback). PDF stays browser print-to-PDF - no client-side
      PDF library, keeping the page dependency-free. *(Batch 6)*
- [x] **Feedback buttons** - 👍/👎 on every answer; POST
      /api/sessions/<id>/feedback stores the rating on the transcript's answer
      entry in SQLite (so it hydrates on reload and dies with a regenerated
      answer) and logs to stderr. Re-click clears; switching rating replaces.
      *(Batch 6)*

### 7. State, Errors, & Boundary Constraints
- [ ] **Context window warning** - progress bar tracking conversation + vault-context
      token usage against the model's limit. *Depends on token telemetry (§4).*
- [x] **Network & rate-limit handling** - inline error bubbles now carry a Retry
      button that re-sends the exact failed payload (message or regenerate);
      connection failures, HTTP errors, and agent errors all route through it.
      *(Batch 2)*
- [x] **Scroll-to-bottom anchor** - floating ↓ button appears when scrolled up
      (sticky-scroll disengages); click snaps back and re-engages following.
      *(Batch 2)*

### 8. Artifacts & Isolated Workspace
- [ ] **Split-screen canvas** - large code/documents open in a right-hand panel
      beside the chat.
- [ ] **Artifact version history** - scrub through iterations of a canvas document.

### 9. Deep Sidebar & History Management
- [x] **Global history search** - search box atop the sidebar filters sessions by
      title/body keywords (debounced; GET /api/sessions?q= does an escaped
      case-insensitive LIKE over title + stored transcript; Esc clears; stale
      responses are dropped so fast typing can't race). *(Batch 5)*
- [x] **Session pinning** - 📌 hover action toggles `pinned` (PATCH; SQLite column
      with auto-migration for pre-existing DBs); pinned sessions sort first under
      a "Pinned" header and survive chat-turn upserts. *(Batch 5)*
- [x] **Time bucketing** - "Pinned / Today / Yesterday / Previous 7 days /
      Previous 30 days / Older" section headers, computed client-side from
      last_used; searching shows a flat "Results" list instead. *(Batch 5)*

### 10. Model Control & Settings Engine
- [ ] **Live model switcher** - change `LLM_MODEL` per-session from a top-bar
      dropdown without losing conversation state.
- [ ] **Persona / behavioral sliders** - settings modal for temperature and system
      prompt profile. *Backend: run_loop must accept and pass through generation
      params.*
