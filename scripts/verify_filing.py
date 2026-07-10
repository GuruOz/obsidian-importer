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

# "Work Log - 09 Jul 2026" style date line inside a staged digest body. The body
# is html2text output, so the hyphen may arrive escaped ("\-") or as an en/em dash.
_WORK_LOG_RE = re.compile(
    r"Work Log\s*(?:\\?-|–|—)?\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})",
    re.IGNORECASE)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def digest_work_dates(staging_dir):
    """Work dates the staged digest batch claims to describe, in file order.

    Derived from the digest content itself (each bundled email states its own
    "Work Log - <DD Mon YYYY>" date), so the check doesn't depend on the agent
    self-reporting correctly - or reporting at all."""
    text = _read(os.path.join(staging_dir, "digest.md"))
    dates = []
    for day, mon, year in _WORK_LOG_RE.findall(text):
        m = _MONTHS.get(mon[:3].lower())
        if not m:
            continue
        iso = f"{int(year):04d}-{m:02d}-{int(day):02d}"
        if iso not in dates:
            dates.append(iso)
    return dates


def staged_note_ids(staging_dir):
    """Message-IDs of ad-hoc note sections in the staged batch. Only those
    sections carry a 'Message-ID:' header line (fetch_digest.message_to_markdown)."""
    text = _read(os.path.join(staging_dir, "digest.md"))
    return re.findall(r"(?m)^Message-ID:\s*(\S+)", text)


def has_digest_section(staging_dir):
    """Whether the staged batch contains an actual daily-summary digest
    (vs. being entirely ad-hoc notes, which never carry a copilot-digest
    marker/date of their own)."""
    return "## Digest received" in _read(os.path.join(staging_dir, "digest.md"))


def _norm_title(s):
    """Normalize a note title for fuzzy matching: casefold, treat spaces,
    underscores and hyphens as one separator, drop other punctuation."""
    s = re.sub(r"[ _\-]+", " ", s.casefold())
    s = re.sub(r"[^\w ]", "", s)
    return s.strip()


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


def check_markers(source, status, work_dates, staging_dir, vault_dir, warn):
    if status in ("skipped_duplicate", "no_content"):
        return
    if source == "digest":
        # A batch may bundle digests for several days (a late send plus the
        # current day); every stated date must have its own daily note + marker.
        for work_date in work_dates:
            matches = glob.glob(os.path.join(vault_dir, "Daily jounal", f"{work_date}*.md"))
            if not matches:
                warn(f"no daily note found for work date {work_date}")
                continue
            marker = f"<!-- copilot-digest:{work_date} -->"
            if not any(marker in _read(m) for m in matches):
                warn(f"idempotency marker {marker} not found in the {work_date} daily note")
        # Ad-hoc note sections are keyed per message, not per date.
        note_ids = staged_note_ids(staging_dir)
        if note_ids:
            haystack = _daily_notes_text(vault_dir)
            for mid in note_ids:
                if f"<!-- work-note:{mid} -->" not in haystack:
                    warn(f"ad-hoc note {mid} left no work-note marker in any daily note "
                         "(filing may have silently failed)")
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


def check_links(files_touched, vault_dir, warn, fixed):
    """Flag dangling links; auto-repair a wikilink whose target differs from
    exactly one existing note only by separators/punctuation (e.g. the agent
    wrote 'TrackingID 123' where the note is 'TrackingID_123'). Ambiguous or
    genuinely unresolvable targets are warned about, never guessed at."""
    notes = _vault_notes(vault_dir)
    by_rel = {p.lower() for p in notes}
    by_base = {os.path.basename(p)[:-3].lower() for p in notes}  # name without .md
    norm_map = {}
    for p in notes:
        base = os.path.basename(p)[:-3]
        norm_map.setdefault(_norm_title(base), set()).add(base)
    dangling = []
    for rel in files_touched:
        abs_path = os.path.join(vault_dir, rel)
        if not os.path.isfile(abs_path):
            continue
        text = _read(abs_path)
        new_text = text
        for raw in set(_WIKILINK_RE.findall(text)):
            name = raw.strip()
            if not name:
                continue
            # Attachment embeds ([[IMG_1.webp]], [[scan.pdf]]) live outside the
            # note index (Attachments/ is excluded) - never note targets, skip.
            if re.search(r"\.[A-Za-z0-9]{2,5}$", name) and not name.lower().endswith(".md"):
                continue
            base = name.split("/")[-1]
            if base.lower() in by_base or name.lower() in by_rel:
                continue
            candidates = norm_map.get(_norm_title(base), set())
            if len(candidates) == 1:
                new_base = next(iter(candidates))
                new_raw = raw.replace(base, new_base)
                # Anchor on the wikilink opener and require the target to end
                # right after (at ]], an alias |, or a #/^ anchor) so a link
                # that merely PREFIXES a longer title is never rewritten.
                pattern = re.compile(r"\[\[" + re.escape(raw) + r"(?=[\]|#^])")
                new_text = pattern.sub("[[" + new_raw, new_text)
                fixed.append((rel, name, new_base))
            else:
                dangling.append((rel, name))
        for m in _MDLINK_RE.findall(text):
            t = re.sub(r"\.md$", "", m.split("/")[-1])
            if t and t.lower() not in by_base and m.lower() not in by_rel:
                dangling.append((rel, t))
        if new_text != text:
            try:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(new_text)
            except OSError as e:
                warn(f"could not write link repair to {rel}: {e}")
    for rel, name, new_base in fixed:
        print(f"verify: fixed link '[[{name}]]' -> '[[{new_base}]]' in {rel}",
              file=sys.stderr, flush=True)
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


def append_filing_log(filing_log, n_warnings, warnings, n_fixed=0):
    if not filing_log or not os.path.isfile(filing_log):
        return  # nothing to attach to (e.g. dry run wrote no Filing Log entry)
    fixed_note = f" ({n_fixed} link(s) auto-repaired)" if n_fixed else ""
    if n_warnings == 0:
        line = f"    - verify: OK{fixed_note}\n"
    else:
        summary = "; ".join(warnings[:3])
        more = f" (+{n_warnings - 3} more)" if n_warnings > 3 else ""
        line = f"    - verify: {n_warnings} warning(s): {summary}{more}{fixed_note}\n"
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

    reported = result.get("work_dates") or result.get("work_date") or []
    if not isinstance(reported, list):
        reported = [reported]
    reported = [str(d) for d in reported if d]

    # Dates that actually carry a daily-summary digest and so are expected to
    # have a copilot-digest marker. An explicit WORK_DATE override (manual
    # backfill) forces the whole batch to one date. Trust the staged batch's
    # own stated "Work Log - <date>" lines over the agent's self-report where
    # possible (they exist even when the agent never called finish); only fall
    # back to the report if real digest content failed to parse a date. A
    # batch that is entirely ad-hoc notes has no digest section at all, so no
    # date should be marker-checked for it.
    if os.environ.get("WORK_DATE"):
        digest_dates = [os.environ["WORK_DATE"]]
    elif args.source == "digest":
        digest_dates = digest_work_dates(args.staging_dir)
        if not digest_dates and has_digest_section(args.staging_dir):
            digest_dates = reported
    else:
        digest_dates = []

    # Broader date set (digest dates plus ad-hoc-note-only dates) used only to
    # find fallback link-check targets when the agent reported no files_touched.
    all_dates = sorted(set(digest_dates) | set(reported))

    files_touched = [_norm_rel(p, args.vault_dir) for p in (result.get("files_touched") or [])]
    if not files_touched and args.source == "digest" and not args.dry_run:
        # Agent gave no final summary; the daily notes for the batch's dates are
        # the guaranteed touch targets, so link checks/repairs still run there.
        for wd in all_dates:
            for p in glob.glob(os.path.join(args.vault_dir, "Daily jounal", f"{wd}*.md")):
                files_touched.append(_norm_rel(p, args.vault_dir))

    warnings = []
    fixed = []

    def warn(msg):
        warnings.append(msg)
        print(f"verify: WARNING - {msg}", file=sys.stderr, flush=True)

    try:
        if args.dry_run:
            check_dry_run(args.source, args.staging_dir, result, warn)
        else:
            pre_paths = _pre_run_paths(args.staging_dir)
            check_markers(args.source, status, digest_dates, args.staging_dir, args.vault_dir, warn)
            check_links(files_touched, args.vault_dir, warn, fixed)
            check_duplicates(files_touched, pre_paths, args.vault_dir, warn)
            check_coverage(args.source, status, args.staging_dir, result, warn)
            append_filing_log(args.filing_log, len(warnings), warnings, len(fixed))
    except Exception as e:  # noqa: BLE001 - verification must never break the run
        print(f"verify: check skipped due to error: {e}", file=sys.stderr, flush=True)

    if warnings:
        print(f"verify: {len(warnings)} warning(s) for {args.source} run.", file=sys.stderr, flush=True)
    else:
        print(f"verify: OK ({args.source}, {status}).", file=sys.stderr, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
