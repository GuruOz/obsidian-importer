#!/usr/bin/env python3
"""One-off maintenance tool: reconstruct missing Filing Log entries from agent
run logs that never got logged live (e.g. runs from before the "unreported"
fix in commit e905b47, where a run that hit its turn limit without calling
finish left nothing in the Filing Log even though it may have filed content).

Safe to re-run: a calendar date already represented by a line in the Filing
Log (live or previously backfilled) is left untouched, so running this
against the same log directory twice never creates duplicate lines. Each
reconstructed line is tagged "(backfilled)" so it stays distinguishable from
a line written live at the time of the run.

Usage:
  backfill_filing_log.py --source <digest|personal>
      [--log-dir /work/logs] [--vault-dir /vault] [--filing-log PATH]
      [--dry-run]

Run inside the pipeline container, e.g.:
  docker compose exec pipeline python3 scripts/backfill_filing_log.py --source digest
"""
import argparse
import glob
import os
import re
import sys
import urllib.parse
from datetime import datetime

import filing_report

MAX_NOTES_IN_SUMMARY = 8

ARCHIVE_SUBDIR = {"digest": "Raw Digests", "personal": "Raw Email"}

_LOG_DATE_RE = re.compile(r"agent\.\w+\.(\d{4}-\d{2}-\d{2})\.json$")
_ENTRY_DATE_RE = re.compile(r"(?m)^- (\d{4}-\d{2}-\d{2}) \d{2}:\d{2}")


def covered_dates(filing_log):
    if not os.path.isfile(filing_log):
        return set()
    with open(filing_log, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    return set(_ENTRY_DATE_RE.findall(text))


def build_line(result, run_date):
    """Same shape as filing_report.py's live line, tagged as backfilled."""
    status = result.get("status", "unknown")
    work_dates = result.get("work_dates") or []
    if not isinstance(work_dates, list):
        work_dates = [work_dates]
    if not work_dates and result.get("work_date"):
        work_dates = [result.get("work_date")]
    work_dates = [str(d) for d in work_dates if d]
    date_label = "work dates" if len(work_dates) > 1 else "work date"

    entries = result.get("entries_filed")
    skipped = result.get("skipped")
    skipped_details = [str(s) for s in (result.get("skipped_details") or [])]
    files = [str(p) for p in (result.get("files_touched") or [])]

    links = ", ".join(filing_report.vault_link(p) for p in files) or "(no notes listed)"
    stamp = f"{run_date} 00:00"
    line = f"- {stamp} — **{status}** *(backfilled)*"
    if work_dates:
        line += f" — {date_label} {', '.join(work_dates)}"
    if entries is not None:
        line += f" — {entries} entries"
    if skipped is not None:
        line += f" — {skipped} skipped"
    line += f" — {links}\n"
    for reason in skipped_details:
        line += f"    - skipped: {reason}\n"
    return line


def build_unreported_line(run_date):
    return (f"- {run_date} 00:00 — **unreported** *(backfilled)* — agent gave no final "
            f"summary (likely hit its turn limit; see the agent log)\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["digest", "personal"])
    ap.add_argument("--log-dir", default=os.environ.get("LOG_DIR", "/work/logs"))
    ap.add_argument("--vault-dir", default=os.environ.get("VAULT_DIR", "/vault"))
    ap.add_argument("--filing-log")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    filing_log = args.filing_log or os.path.join(
        args.vault_dir, ARCHIVE_SUBDIR[args.source], "Filing Log.md")

    seen = covered_dates(filing_log)
    pattern = os.path.join(args.log_dir, f"agent.{args.source}.*.json")
    logs = sorted(glob.glob(pattern))
    if not logs:
        print(f"No agent logs found matching {pattern}")
        return

    to_write = []
    for log_path in logs:
        m = _LOG_DATE_RE.search(os.path.basename(log_path))
        if not m:
            print(f"skip (unrecognized filename): {log_path}")
            continue
        run_date = m.group(1)
        if run_date in seen:
            print(f"skip (already logged): {run_date}")
            continue
        result = filing_report.last_status_json(log_path)
        if not result:
            print(f"backfill (unreported): {run_date}")
            to_write.append(build_unreported_line(run_date))
            continue
        status = result.get("status", "unknown")
        if status in ("skipped_duplicate", "no_content"):
            print(f"skip (no-op run, nothing to log): {run_date} [{status}]")
            continue
        print(f"backfill: {run_date} [{status}]")
        to_write.append(build_line(result, run_date))

    if not to_write:
        print("Nothing to backfill.")
        return

    print(f"\n{len(to_write)} entry(ies) to append to {filing_log}:\n")
    print("".join(to_write))

    if args.dry_run:
        print("(dry run - nothing written)")
        return

    os.makedirs(os.path.dirname(filing_log), exist_ok=True)
    is_new = not os.path.exists(filing_log)
    with open(filing_log, "a", encoding="utf-8") as f:
        if is_new:
            f.write("# Filing Log\n\nOne line per pipeline run that changed the vault. "
                     "Maintained automatically by the ingestion pipeline.\n\n")
        f.write("".join(to_write))
    print(f"Wrote {len(to_write)} entry(ies) to {filing_log}")


if __name__ == "__main__":
    main()
