# Plan: post-ingestion stitch pass + retrieval hygiene

Decision date: 2026-07-13. **Status: implemented 2026-07-13** (both phases; the
stitch pass ships in report-only mode - flip `STITCH_APPLY_LINKS=1` in .env once
the reports look right). Driven by one question: **does a nightly cleanup make
the chat better?** The chat is the product; the vault is its database. Anything
that doesn't improve retrieval quality is not worth an agent run.

## Verdict

| Idea | Verdict | Why |
| --- | --- | --- |
| Compress/summarize notes to keep the vault "concise" | **Rejected** | Retrieval is chunked (BM25 + embeddings over ~500-word chunks), so long dense notes retrieve precisely. Density costs nothing; destructive rewrites risk the archive and buy no retrieval quality. Keep the vault dense. |
| Nightly whole-vault re-audit ("was everything filed right?") | **Rejected** | Duplicates verify_filing.py's deterministic per-run checks at LLM cost, over an ever-growing corpus. Audit must stay bounded to the last 24h. |
| Demote raw archives in chat retrieval | **Do first** | Raw Digests / Raw Email / Raw Chats are indexed today and recency-boosted, so fresh raw dumps outrank distilled notes on exactly the queries that matter. Deterministic fix, no agent, immediate chat improvement. |
| Nightly stitch/correlation pass | **Do second** | The one gap agents actually leave: the same story filed under different titles across sources never converges. Fragmentation makes the chat assemble answers from half the evidence. Bounded to notes touched in the last 24h. |

User decisions locked in (2026-07-13):
- Raw archives: **demote, don't exclude** (chat can still quote raw transcripts when asked).
- Stitch authority: **cross-links applied live; note merges proposal-only.**

## Phase 1 — retrieval demotion of raw archives (no agent, ship first)

- `lexical_index.py`: multiply a chunk's score by `RAW_ARCHIVE_DEMOTE` (default
  `0.4`, env-tunable) when its path is under a raw archive folder
  (`Raw Digests/`, `Raw Email/`, `Raw Chats/` — one shared constant).
  Relevance stays primary: a raw chunk that matches far better still wins.
- `semantic_index.py`: apply the same demotion to the cosine ranking before
  Reciprocal-Rank-Fusion so both legs agree (otherwise RRF re-inflates raw hits).
- Side benefit: the nightly ingestion agents share this index via
  `search_relevant`, and they *should* prefer topic notes over raw dumps when
  choosing filing targets — demotion helps them too.
- Excluded folders (`Attachments`, `smart-chats`) unchanged.
- Verification: ask the chat a question answered by a distilled note that
  currently loses to a same-day raw dump; the note should now lead. Filing Log
  citations in chat answers should shift from `Raw */...` toward topic notes.

## Phase 2 — nightly stitch pass

One new source-shaped job, reusing the existing orchestration idioms (flock,
dry-run-first, deferred effects, ntfy, dashboard logs).

### Schedule
- `crontab`: `30 5 * * * /app/scripts/run-stitch.sh >> /work/logs/cron.log 2>&1`
  (05:30 SGT: after the 23:30 WhatsApp run has long finished, before the day
  starts; avoids the midnight date rollover mid-run).
- Takes the same `/work/ingest.lock` flock as every other job, so it can never
  overlap an ingestion (and the existing restart guard covers it for free).

### Scope guard (keeps cost flat as the vault grows)
- Inputs are only: (a) notes with mtime in the last 24h from the vault index,
  (b) the last-24h tails of all four Filing Logs, (c) last night's per-source
  agent logs' finish summaries. If (a) is empty → exit 20, "nothing to stitch".
- Caps: `STITCH_MAX_TOPICS` (default 20 touched notes examined),
  `STITCH_MAX_LOOPS` (default 40 agent turns).

### What the agent does (prompt_stitch.txt)
1. For each note touched last night, `search_relevant` the vault for existing
   notes covering the same workstream under other names (the demoted index from
   Phase 1 makes these lookups cleaner).
2. **Cross-links (applied live):** where two notes are clearly the same story
   and neither links the other, append a single `See also: [[Exact Title]]`
   line to each (exact-title rule, same as the filing prompts). Append-only —
   never rewrite, reorder, or delete, same contract as every other agent.
3. **Merge proposals (never applied):** where notes look like true duplicates
   (parallel notes that should be one), write the proposal to
   `/work/staging/stitch/proposed.md` — which notes, merged title, section
   mapping — for review in the dashboard (add it to `PROPOSED_FILES`).
4. **Bounded audit:** flag (report-only) last night's oddities: sections
   SKIPped with reasons that look wrong, agent runs that hit the loop cap
   without calling finish, filings into a brand-new note whose title
   near-matches an existing one.
5. Finish tool reports `{links_added, merge_candidates, audit_flags}`.

### Outputs
- Vault: only the appended `See also:` lines. No report notes in the vault —
  reports are pipeline exhaust, not knowledge.
- `/work/logs/agent.stitch.<date>.json` (dashboard Logs tab picks it up).
- ntfy one-liner: "Stitch: 3 links, 1 merge candidate, 0 flags."
- `chown -R 1000:1000` after vault writes, like run-ingest.sh.

### Safety & rollout
- `STITCH_APPLY_LINKS=0` default for the first week: everything (links too)
  goes to the report only. Flip to 1 once the proposals look right.
- Deterministic post-check (verify_filing-style, warn-only): every note the
  agent touched must differ from the pre-run snapshot by appended lines only —
  any in-place change is flagged loudly.
- The pre-run vault snapshot in run-ingest.sh is reused verbatim here.

## Success criteria
- Chat answers cite topic notes rather than raw archives except when the
  question is about verbatim conversation.
- Stitch reports trend toward zero findings — meaning the filing prompts are
  converging on their own and the stitch pass has caught the backlog. If it
  keeps finding fragmentation on the same workstreams, that's a signal to
  improve the filing prompts (better create-vs-update hints), not to grow the
  stitch pass.

## Explicitly out of scope
- Any deletion, compression, or rewriting of existing notes.
- Whole-vault sweeps of any kind on a schedule.
- Merging notes without human approval.
