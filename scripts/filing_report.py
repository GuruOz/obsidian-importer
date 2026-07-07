#!/usr/bin/env python3
"""Turn an agent run log into a one-line audit summary.

Usage: filing_report.py <agent_log> [--vault-log <path>]

Scans the log for the agent's final `finish` JSON (a line like
{"status": "filed", "work_date": ..., "files_touched": [...], ...}), prints a short
human-readable summary to stdout (used in the ntfy notification), and - when
--vault-log is given - appends a wikilinked audit line to that note in the vault.

Best-effort by design: if the log has no parseable summary, prints nothing and
exits 0 so the orchestrator can fall back to its generic notification text.
"""
import json
import os
import sys
import urllib.parse
from datetime import datetime

MAX_NOTES_IN_SUMMARY = 8


def vault_link(path):
    """Obsidian link for a vault-relative note path.

    Plain [[name]] wikilinks silently break when the note name contains
    wikilink syntax characters - '#' starts a heading anchor, '^' a block
    anchor, '|' an alias - e.g. [[X - TrackingID#123]] is parsed as note "X -
    TrackingID" + heading "123" and never resolves. For those names fall back
    to a percent-encoded markdown link, which Obsidian resolves with the same
    vault-wide shortest-path lookup."""
    p = str(path).replace("\\", "/").strip("/")
    name = note_name(p)
    if not any(c in name for c in "#^|"):
        return f"[[{name}]]"
    target = p if p.endswith(".md") else f"{p}.md"
    return f"[{name}]({urllib.parse.quote(target)})"


def last_status_json(log_path):
    found = None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and "status" in obj:
                    found = obj
    except OSError:
        return None
    return found


def note_name(path):
    base = os.path.basename(str(path))
    return base[:-3] if base.endswith(".md") else base


def main():
    if len(sys.argv) < 2:
        sys.exit(2)
    log_path = sys.argv[1]
    vault_log = None
    if "--vault-log" in sys.argv:
        i = sys.argv.index("--vault-log")
        if i + 1 < len(sys.argv):
            vault_log = sys.argv[i + 1]

    result = last_status_json(log_path)
    if not result:
        return  # nothing parseable; orchestrator falls back to generic text

    status = result.get("status", "unknown")
    work_date = result.get("work_date", "")
    entries = result.get("entries_filed")
    skipped = result.get("skipped")
    skipped_details = [str(s) for s in (result.get("skipped_details") or [])]
    files = [str(p) for p in (result.get("files_touched") or [])]

    parts = [status]
    if work_date:
        parts.append(f"work date {work_date}")
    if entries is not None:
        parts.append(f"{entries} entries")
    if skipped is not None:
        parts.append(f"{skipped} skipped")
    if files:
        shown = [note_name(p) for p in files[:MAX_NOTES_IN_SUMMARY]]
        more = len(files) - len(shown)
        notes = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
        parts.append(f"notes: {notes}")
    print(" | ".join(parts))

    if vault_log and status not in ("skipped_duplicate", "no_content"):
        links = ", ".join(vault_link(p) for p in files) or "(no notes listed)"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"- {stamp} — **{status}**"
        if work_date:
            line += f" — work date {work_date}"
        if entries is not None:
            line += f" — {entries} entries"
        if skipped is not None:
            line += f" — {skipped} skipped"
        line += f" — {links}\n"
        # Record triage decisions as indented sub-bullets so the audit trail shows
        # exactly what was skipped and why - you audit the judgment, not trust it.
        for reason in skipped_details:
            line += f"    - skipped: {reason}\n"
        os.makedirs(os.path.dirname(vault_log), exist_ok=True)
        is_new = not os.path.exists(vault_log)
        with open(vault_log, "a", encoding="utf-8") as f:
            if is_new:
                f.write("# Filing Log\n\nOne line per pipeline run that changed the vault. "
                        "Maintained automatically by the ingestion pipeline.\n\n")
            f.write(line)


if __name__ == "__main__":
    main()
