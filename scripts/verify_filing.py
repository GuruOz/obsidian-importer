#!/usr/bin/env python3
"""Deterministic, warn-only sanity checks on what the filing agent just did.

Called by run-ingest.sh after the agent step, before the ledger commit. Never
fails the run (always exits 0): it surfaces problems the LLM can't be trusted to
self-report - a missing idempotency marker (email staged but silently not filed),
a dangling wikilink, a near-duplicate of an existing note, or a big gap between
source items and filed entries. Findings print as "verify: ..." lines (tee'd into
the run log) and, on a live run, a summary sub-bullet is appended to the Filing
Log entry filing_report.py just wrote.

Usage:
  verify_filing.py --source <digest|personal> --staging-dir S --vault-dir V \\
      --agent-log L [--filing-log F] [--dry-run]
"""
import argparse
import difflib
import glob
import os
import re
import sys

import filing_report

# Heuristic per-workstream marker in the Copilot work digest (its body isn't
# formally structured markdown). Tunable here without touching the logic.
DIGEST_SECTION_MARKER = "Source Basis"
DUP_TITLE_RATIO = 0.75
COVERAGE_WARN_FRACTION = 0.5   # warn if digest entries_filed < this * workstreams

_WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")


def _norm_rel(path, vault_dir):
    """A vault-relative, forward-slash path from an absolute or relative one."""
    p = str(path).replace("\\", "/")
    vd = vault_dir.replace("\\", "/").rstrip("/")
    if p.startswith(vd + "/"):
        p = p[len(vd) + 1:]
    return p.lstrip("/")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _vault_notes(vault_dir):
    """All vault-relative .md paths, dot/Attachments/smart-chats excluded."""
    out = []
    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("Attachments", "smart-chats")]
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(root, f), vault_dir).replace("\\", "/"))
    return out


def _daily_notes_text(vault_dir):
    """Concatenated text of every note under 'Daily jounal/' (for marker scans)."""
    folder = os.path.join(vault_dir, "Daily jounal")
    text = []
    if os.path.isdir(folder):
        for f in glob.glob(os.path.join(folder, "*.md")):
            text.append(_read(f))
    return "\n".join(text)


def _pre_run_paths(staging_dir):
    """Paths present in the vault_index.txt snapshot taken before the run (strip
    the leading 'YYYY-MM-DD<TAB>' date column)."""
    idx = os.path.join(staging_dir, "vault_index.txt")
    paths = set()
    for line in _read(idx).splitlines():
        line = line.rstrip("\n")
        if "\t" in line:
            line = line.split("\t", 1)[1]
        line = line.strip()
        if line:
            paths.add(line.replace("\\", "/"))
    return paths


def check_markers(source, status, work_date, staging_dir, vault_dir, warn):
    if status in ("skipped_duplicate", "no_content"):
        return
    if source == "digest":
        if not work_date:
            return
        matches = glob.glob(os.path.join(vault_dir, "Daily jounal", f"{work_date}*.md"))
        if not matches:
            warn(f"no daily note found for work date {work_date}")
            return
        marker = f"<!-- copilot-digest:{work_date} -->"
        if not any(marker in _read(m) for m in matches):
            warn(f"idempotency marker {marker} not found in the {work_date} daily note")
    else:  # personal: each pending message id should have left a marker somewhere
        pending = _read(os.path.join(staging_dir, "pending_ids.txt")).split()
        if not pending:
            return
        haystack = _daily_notes_text(vault_dir)
        missing = [mid for mid in pending if f"<!-- personal-email:{mid} -->" not in haystack]
        # Some pending ids are legitimately skipped as noise; only flag when a
        # large share left no marker, which points at silent drops rather than
        # deliberate triage.
        if missing and len(missing) == len(pending):
            warn(f"none of the {len(pending)} staged email(s) left a personal-email marker "
                 "(all triaged out, or filing silently failed)")
        elif len(missing) > max(2, len(pending) // 2):
            warn(f"{len(missing)}/{len(pending)} staged email(s) left no marker "
                 "(unexpected if they were meant to be filed)")


def check_links(files_touched, vault_dir, warn):
    notes = _vault_notes(vault_dir)
    by_rel = {p.lower() for p in notes}
    by_base = {os.path.basename(p)[:-3].lower() for p in notes}  # name without .md
    dangling = []
    for rel in files_touched:
        abs_path = os.path.join(vault_dir, rel)
        if not os.path.isfile(abs_path):
            continue
        text = _read(abs_path)
        targets = set(_WIKILINK_RE.findall(text))
        for m in _MDLINK_RE.findall(text):
            targets.add(re.sub(r"\.md$", "", m.split("/")[-1]))
        for t in targets:
            t = t.strip()
            if not t:
                continue
            name = t[:-3] if t.lower().endswith(".md") else t
            base = name.split("/")[-1].lower()
            if base in by_base or name.lower() in by_rel:
                continue
            dangling.append((rel, t))
    for rel, t in dangling[:15]:
        warn(f"dangling link [[{t}]] in {rel} resolves to no note")
    if len(dangling) > 15:
        warn(f"...and {len(dangling) - 15} more dangling link(s)")


def check_duplicates(files_touched, pre_paths, vault_dir, warn):
    existing = [p for p in _vault_notes(vault_dir)]
    by_folder = {}
    for p in existing:
        by_folder.setdefault(os.path.dirname(p), []).append(os.path.basename(p)[:-3])
    new_notes = [rel for rel in files_touched
                 if rel not in pre_paths and os.path.isfile(os.path.join(vault_dir, rel))]
    for rel in new_notes:
        folder, base = os.path.dirname(rel), os.path.basename(rel)[:-3]
        for other in by_folder.get(folder, []):
            if other == base:
                continue
            if difflib.SequenceMatcher(None, other.lower(), base.lower()).ratio() > DUP_TITLE_RATIO:
                warn(f"new note '{base}' looks like a near-duplicate of existing "
                     f"'{other}' in {folder or '(root)'}")
                break


def check_coverage(source, status, staging_dir, result, warn):
    if source == "personal":
        staged = _read(os.path.join(staging_dir, "personal.md"))
        n_emails = len(re.findall(r"(?m)^## Email \d+:", staged))
        filed = result.get("entries_filed")
        skipped = result.get("skipped")
        if n_emails and filed is not None and skipped is not None:
            if filed + skipped != n_emails:
                warn(f"coverage mismatch: {n_emails} staged email(s) but "
                     f"{filed} filed + {skipped} skipped = {filed + skipped}")
    else:  # digest: heuristic workstream count
        staged = _read(os.path.join(staging_dir, "digest.md"))
        n_ws = staged.count(DIGEST_SECTION_MARKER)
        filed = result.get("entries_filed")
        if status in ("skipped_duplicate", "no_content"):
            return
        if n_ws and filed is not None and filed < COVERAGE_WARN_FRACTION * n_ws:
            warn(f"coverage: digest has ~{n_ws} workstream(s) ('{DIGEST_SECTION_MARKER}') "
                 f"but only {filed} entr(y/ies) filed - check nothing was dropped")


def check_dry_run(source, staging_dir, result, warn):
    """Lightweight coverage check against the proposal instead of the vault."""
    proposed = _read(os.path.join(staging_dir, "proposed.md"))
    if not proposed.strip():
        warn("proposed.md is empty")
        return
    if source == "personal":
        staged = _read(os.path.join(staging_dir, "personal.md"))
        n_emails = len(re.findall(r"(?m)^## Email \d+:", staged))
        filed = result.get("entries_filed") or 0
        skipped = result.get("skipped") or 0
        if n_emails and (filed + skipped) != n_emails:
            warn(f"coverage: {n_emails} staged email(s) but proposal accounts for "
                 f"{filed} filed + {skipped} skipped")
    else:
        staged = _read(os.path.join(staging_dir, "digest.md"))
        n_ws = staged.count(DIGEST_SECTION_MARKER)
        filed = result.get("entries_filed") or 0
        if n_ws and filed < COVERAGE_WARN_FRACTION * n_ws:
            warn(f"coverage: digest has ~{n_ws} workstream(s) but proposal files {filed}")


def append_filing_log(filing_log, n_warnings, warnings):
    if not filing_log or not os.path.isfile(filing_log):
        return  # nothing to attach to (e.g. dry run wrote no Filing Log entry)
    if n_warnings == 0:
        line = "    - verify: OK\n"
    else:
        summary = "; ".join(warnings[:3])
        more = f" (+{n_warnings - 3} more)" if n_warnings > 3 else ""
        line = f"    - verify: {n_warnings} warning(s): {summary}{more}\n"
    try:
        with open(filing_log, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["digest", "personal"])
    ap.add_argument("--staging-dir", required=True)
    ap.add_argument("--vault-dir", required=True)
    ap.add_argument("--agent-log", required=True)
    ap.add_argument("--filing-log")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    result = filing_report.last_status_json(args.agent_log) or {}
    status = result.get("status", "unknown")
    work_date = result.get("work_date", "")
    files_touched = [_norm_rel(p, args.vault_dir) for p in (result.get("files_touched") or [])]

    warnings = []

    def warn(msg):
        warnings.append(msg)
        print(f"verify: WARNING - {msg}", file=sys.stderr, flush=True)

    try:
        if args.dry_run:
            check_dry_run(args.source, args.staging_dir, result, warn)
        else:
            pre_paths = _pre_run_paths(args.staging_dir)
            check_markers(args.source, status, work_date, args.staging_dir, args.vault_dir, warn)
            check_links(files_touched, args.vault_dir, warn)
            check_duplicates(files_touched, pre_paths, args.vault_dir, warn)
            check_coverage(args.source, status, args.staging_dir, result, warn)
            append_filing_log(args.filing_log, len(warnings), warnings)
    except Exception as e:  # noqa: BLE001 - verification must never break the run
        print(f"verify: check skipped due to error: {e}", file=sys.stderr, flush=True)

    if warnings:
        print(f"verify: {len(warnings)} warning(s) for {args.source} run.", file=sys.stderr, flush=True)
    else:
        print(f"verify: OK ({args.source}, {status}).", file=sys.stderr, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
