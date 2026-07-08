#!/usr/bin/env python3
"""Stage new personal-inbox emails (since a watermark) as one combined markdown
file for the triage-and-file agent step.

Design mirrors fetch_digest.py but for the whole Inbox instead of one sender:

* First run seeds a watermark at now - PERSONAL_MAIL_LOOKBACK_DAYS (default 0,
  i.e. "start from now": nothing before today is ever ingested). Changing the
  lookback later rewinds the watermark (never forward) so older mail gets
  backfilled; the ledger keeps already-filed emails from being re-filed.
* Every run lists Inbox messages with receivedDateTime >= watermark (staging
  capped at PERSONAL_MAIL_MAX_PER_RUN, oldest-first, so a backlog drains across
  nights and a flood never blows the context budget), skips the Copilot digest
  sender and anything already ledgered - following extra listing pages when a
  window is dominated by skipped messages - fetches bodies, and stages one
  combined markdown file.
* Watermark and ledger both advance only after the whole run succeeds: the
  fetcher stages pending_ids.txt and pending_watermark.txt, and run-ingest.sh
  commits them only once the agent filing step has succeeded. A failed run
  therefore re-picks-up the same emails next night instead of losing them.
* Dry runs (DRY_RUN=1) never persist anything: no watermark seed/rewind/advance
  and no lookback state, so a dry run leaves the pipeline exactly as it found it.
* PERSONAL_MAIL_START_DATE / PERSONAL_MAIL_END_DATE (YYYY-MM-DD, local time,
  end inclusive) switch the run to an explicit-window backfill: the watermark
  is bypassed and left untouched, and only the ledger records progress.

Exit codes (consumed by run-ingest.sh):
    0   staged one or more emails successfully
    1   unexpected/unhandled error
    20  nothing new to stage (incl. first-run watermark init) - benign skip
    30  Graph auth failed - needs graph_auth.py re-run
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from graph_mail import (
    GRAPH_BASE,
    env,
    get_access_token,
    graph_get,
    load_ledger,
    html_to_markdown,
    resolve_folder_id,
)

# Per-email body cap. Long threads/quoted history are truncated with a notice so
# a single verbose email can't dominate the agent's context budget.
BODY_CHAR_CAP = 12000

# Listing pages followed per run. Skipped messages (digest sender, already
# ledgered) don't count against max_per_run, so during a lookback backfill the
# listing may need several pages to find max_per_run stageable emails; this
# bounds the Graph calls a single night can make.
MAX_LIST_PAGES = 10


def utc_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_iso():
    return utc_iso(datetime.now(timezone.utc))


def lookback_start_iso(days):
    return utc_iso(datetime.now(timezone.utc) - timedelta(days=days))


def read_watermark(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip() or None


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_lookback_days():
    raw = (env("PERSONAL_MAIL_LOOKBACK_DAYS", "0") or "0").strip()
    try:
        days = int(raw)
    except ValueError:
        print(
            f"Invalid PERSONAL_MAIL_LOOKBACK_DAYS={raw!r}; falling back to 0 (start from now).",
            file=sys.stderr,
        )
        return 0
    return max(days, 0)


def resolve_watermark(watermark_file, lookback_days, dry_run):
    """Return the effective watermark, seeding or rewinding it for the lookback.

    * No watermark yet (first run): seed it at now - lookback_days and commit
      immediately (there is nothing pending to defer behind).
    * Lookback setting changed since it was last applied: rewind the watermark
      to now - lookback_days if that is earlier - never forward, so unprocessed
      mail is never skipped. The ledger keeps backfilled-but-already-filed
      emails from being re-filed. The last-applied value lives next to the
      watermark so an unchanged setting doesn't rewind (and re-list the whole
      window) every night.

    A dry run computes the same effective watermark but never writes it (or the
    lookback state) to disk - a dry run must leave no trace, so a later live
    run sees exactly the same mail.
    """
    lookback_state_file = watermark_file + ".lookback"
    applied_raw = read_watermark(lookback_state_file)

    watermark = read_watermark(watermark_file)
    if watermark is None:
        watermark = lookback_start_iso(lookback_days)
        if dry_run:
            print(f"First run (dry run): using watermark {watermark} "
                  f"({lookback_days} day(s) back) without persisting it.")
        else:
            write_text(watermark_file, watermark)
            write_text(lookback_state_file, str(lookback_days))
            print(f"First run: watermark initialised at {watermark} ({lookback_days} day(s) back).")
        return watermark

    if applied_raw != str(lookback_days):
        candidate = lookback_start_iso(lookback_days)
        # Both are UTC "%Y-%m-%dT%H:%M:%SZ" strings, so lexicographic order is
        # chronological order.
        if candidate < watermark:
            print(f"Lookback changed to {lookback_days} day(s): rewinding watermark "
                  f"{watermark} -> {candidate}{' (dry run: not persisted)' if dry_run else ''}.")
            watermark = candidate
            if not dry_run:
                write_text(watermark_file, watermark)
        if not dry_run:
            write_text(lookback_state_file, str(lookback_days))
    return watermark


def read_window():
    """Parse the optional explicit ingestion window from
    PERSONAL_MAIL_START_DATE / PERSONAL_MAIL_END_DATE (YYYY-MM-DD, interpreted
    in the container's local timezone, end date inclusive).

    Returns (start_iso, end_exclusive_iso) in UTC, or (None, None) when no
    window is set. When a window is active the watermark is bypassed entirely
    (neither read as the start point nor advanced afterwards) - the run is a
    stateless backfill over exactly that date range, with the ledger still
    preventing double-filing.
    """
    start_raw = (env("PERSONAL_MAIL_START_DATE", "") or "").strip()
    end_raw = (env("PERSONAL_MAIL_END_DATE", "") or "").strip()
    if not start_raw and not end_raw:
        return None, None
    if not start_raw:
        sys.exit("PERSONAL_MAIL_END_DATE requires PERSONAL_MAIL_START_DATE to be set too.")

    def parse_date(raw, name):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").astimezone()
        except ValueError:
            sys.exit(f"{name} must be YYYY-MM-DD, got {raw!r}")

    start_local = parse_date(start_raw, "PERSONAL_MAIL_START_DATE")
    start_iso = utc_iso(start_local.astimezone(timezone.utc))
    end_iso = None
    if end_raw:
        end_local = parse_date(end_raw, "PERSONAL_MAIL_END_DATE")
        if end_local < start_local:
            sys.exit(f"PERSONAL_MAIL_END_DATE ({end_raw}) is before PERSONAL_MAIL_START_DATE ({start_raw}).")
        end_iso = utc_iso((end_local + timedelta(days=1)).astimezone(timezone.utc))
    return start_iso, end_iso


def body_to_markdown(message):
    body = message.get("body", {})
    content = body.get("content", "")
    content_type = body.get("contentType", "text")
    if content_type == "html":
        md = html_to_markdown(content)
    else:
        md = content.strip()
    if len(md) > BODY_CHAR_CAP:
        dropped = len(md) - BODY_CHAR_CAP
        md = md[:BODY_CHAR_CAP].rstrip() + f"\n\n> [truncated: {dropped} more characters omitted]"
    return md


def message_to_section(index, message):
    frm = (message.get("from", {}) or {}).get("emailAddress", {}) or {}
    from_name = frm.get("name", "") or ""
    from_addr = frm.get("address", "") or ""
    from_line = f"{from_name} <{from_addr}>".strip() if from_addr else (from_name or "(unknown sender)")
    subject = message.get("subject", "") or "(no subject)"
    received = message.get("receivedDateTime", "") or ""
    msg_id = message.get("internetMessageId", "") or ""

    header = (
        f"## Email {index}: {subject}\n\n"
        f"- **From:** {from_line}\n"
        f"- **Subject:** {subject}\n"
        f"- **Received:** {received}\n"
        f"- **Message-ID:** {msg_id}\n"
    )
    return f"{header}\n{body_to_markdown(message)}\n"


def main():
    staging_dir = env("STAGING_DIR", "/work/staging/personal", required=True)
    ledger_file = env("LEDGER_FILE", "/work/personal_processed_ids.txt", required=True)
    # run-ingest.sh exports WATERMARK_FILE (resolved once) so the first-run write
    # here and the deferred commit there hit the same path. Fall back to the
    # per-source env var / default when run standalone.
    watermark_file = (
        os.environ.get("WATERMARK_FILE")
        or env("PERSONAL_MAIL_WATERMARK_FILE", "/work/personal_watermark.txt")
    )
    inbox_folder = env("PERSONAL_MAIL_FOLDER", "Inbox")
    # Reuse the digest sender (already configured) so the nightly Copilot digest,
    # handled by its own source, isn't double-filed here.
    digest_from = (env("DIGEST_FROM", "") or "").lower()
    max_per_run = int(env("PERSONAL_MAIL_MAX_PER_RUN", "25"))
    lookback_days = read_lookback_days()
    # run-ingest.sh exports the per-source resolved DRY_RUN; fall back to the
    # personal default (1, safe) when run standalone. Dry runs must not persist
    # any watermark/lookback state - see resolve_watermark().
    dry_run = (os.environ.get("DRY_RUN") or env("PERSONAL_MAIL_DRY_RUN", "1") or "1") == "1"
    window_start, window_end = read_window()

    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "archive"), exist_ok=True)

    print(f"fetch_inbox: folder={inbox_folder!r} max_per_run={max_per_run} "
          f"lookback_days={lookback_days} dry_run={dry_run} staging={staging_dir} ledger={ledger_file}")
    if window_start is not None:
        # Explicit window: a stateless backfill. The watermark is neither used
        # nor touched; the ledger alone prevents double-filing.
        watermark = window_start
        print(f"fetch_inbox: explicit window {window_start} .. {window_end or '(now)'} "
              "- watermark bypassed and left untouched")
    else:
        watermark = resolve_watermark(watermark_file, lookback_days, dry_run)
        print(f"fetch_inbox: effective watermark is {watermark}")

    token = get_access_token()
    ledger = load_ledger(ledger_file)
    print(f"fetch_inbox: ledger holds {len(ledger)} already-processed message id(s)")
    folder_id = resolve_folder_id(token, inbox_folder)

    # List metadata only (no bodies), oldest-first from the watermark so a backlog
    # drains deterministically across nights. `ge` + ledger dedup means the
    # boundary message is re-listed but never re-filed. Skipped messages don't
    # count against max_per_run, so keep following @odata.nextLink until enough
    # stageable emails are found - otherwise a window full of skipped messages
    # (e.g. daily digests on an otherwise-quiet inbox, or ledgered mail during a
    # lookback backfill) would starve, and permanently wedge, the fetch.
    staged = []
    max_seen = watermark  # newest receivedDateTime examined (staged or skipped)
    list_filter = f"receivedDateTime ge {watermark}"
    if window_end is not None:
        list_filter += f" and receivedDateTime lt {window_end}"
    list_path = f"/me/mailFolders/{folder_id}/messages"
    list_params = {
        "$top": max_per_run,
        "$orderby": "receivedDateTime asc",
        "$filter": list_filter,
        "$select": "id,internetMessageId,subject,from,receivedDateTime",
    }
    for page in range(1, MAX_LIST_PAGES + 1):
        listing = graph_get(token, list_path, params=list_params)
        page_msgs = listing.get("value", [])
        print(f"fetch_inbox: page {page}: listed {len(page_msgs)} message(s)")
        for msg in page_msgs:
            received = msg.get("receivedDateTime", "") or ""
            subject = msg.get("subject", "") or "(no subject)"
            if received > max_seen:
                max_seen = received
            from_addr = (msg.get("from", {}) or {}).get("emailAddress", {}).get("address", "")
            if digest_from and from_addr.lower() == digest_from:
                print(f"fetch_inbox:   skip (digest sender) [{received}] {subject!r}")
                continue
            if msg.get("internetMessageId") in ledger:
                print(f"fetch_inbox:   skip (already processed) [{received}] {subject!r}")
                continue
            print(f"fetch_inbox:   stage [{received}] {from_addr}: {subject!r}")
            staged.append(msg)
            if len(staged) >= max_per_run:
                print(f"fetch_inbox: reached max_per_run ({max_per_run}); "
                      "remaining backlog drains on the next run")
                break
        next_link = listing.get("@odata.nextLink", "")
        if len(staged) >= max_per_run or not next_link.startswith(GRAPH_BASE):
            break
        list_path = next_link[len(GRAPH_BASE):]
        list_params = None  # nextLink already carries the query (incl. skiptoken)

    if not staged:
        if max_seen > watermark and window_start is None and not dry_run:
            # Everything examined was deliberately skipped; advance the watermark
            # past it now (nothing is pending behind the agent step) so the same
            # skipped window isn't re-listed every night. Never during a dry run
            # (leave no trace) or an explicit window (stateless backfill).
            write_text(watermark_file, max_seen)
            print(f"Watermark advanced to {max_seen} past {watermark} (all listed messages skipped).")
        print("No new inbox email to stage.")
        sys.exit(20)

    print(f"fetch_inbox: fetching bodies for {len(staged)} staged message(s)...")
    for msg in staged:
        full = graph_get(token, f"/me/messages/{msg['id']}", params={"$select": "body"})
        msg["body"] = full.get("body", {})

    sections = [message_to_section(i + 1, m) for i, m in enumerate(staged)]
    combined = (
        f"# Personal inbox batch — fetched {now_utc_iso()}\n\n"
        f"{len(staged)} email(s) since watermark {watermark}.\n\n---\n\n"
        + "\n---\n\n".join(sections)
    )

    combined_path = os.path.join(staging_dir, "personal.md")
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(combined)

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    archive_path = os.path.join(staging_dir, "archive", f"{today}.md")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(combined)

    # Deferred commit: run-ingest.sh appends these ids to the ledger and advances
    # the watermark only after the agent step succeeds. Advance the watermark to
    # the newest message examined (staged or skipped - listing is oldest-first,
    # so nothing unexamined is left behind it) so the next run continues past
    # this batch.
    pending_ids = os.path.join(staging_dir, "pending_ids.txt")
    with open(pending_ids, "w", encoding="utf-8") as f:
        for msg in staged:
            f.write(msg["internetMessageId"] + "\n")

    pending_watermark = os.path.join(staging_dir, "pending_watermark.txt")
    if window_start is None:
        with open(pending_watermark, "w", encoding="utf-8") as f:
            f.write(max_seen)
    else:
        # Explicit window: never stage a watermark. Committing max_seen (which is
        # inside the window, possibly far in the past) would rewind the nightly
        # watermark and re-list everything after it.
        if os.path.exists(pending_watermark):
            os.remove(pending_watermark)
        print("Explicit window: watermark not staged; ledger alone records progress.")

    print(f"Staged {len(staged)} inbox email(s) to {combined_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_inbox.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
