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
