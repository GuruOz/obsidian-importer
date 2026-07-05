#!/usr/bin/env python3
"""Stage new personal-inbox emails (since a watermark) as one combined markdown
file for the triage-and-file agent step.

Design mirrors fetch_digest.py but for the whole Inbox instead of one sender:

* First run writes a watermark set to "now" and exits benignly (exit 20) - this
  is the "start from now" behaviour: nothing before today is ever ingested.
* Every run after lists Inbox messages with receivedDateTime >= watermark
  (capped, oldest-first, so a backlog drains across nights and a flood never
  blows the context budget), skips the Copilot digest sender and anything
  already ledgered, fetches bodies, and stages one combined markdown file.
* Watermark and ledger both advance only after the whole run succeeds: the
  fetcher stages pending_ids.txt and pending_watermark.txt, and run-ingest.sh
  commits them only once the agent filing step has succeeded. A failed run
  therefore re-picks-up the same emails next night instead of losing them.

Exit codes (consumed by run-ingest.sh):
    0   staged one or more emails successfully
    1   unexpected/unhandled error
    20  nothing new to stage (incl. first-run watermark init) - benign skip
    30  Graph auth failed - needs graph_auth.py re-run
"""
import os
import sys
from datetime import datetime, timezone

from graph_mail import (
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


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_watermark(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip() or None


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

    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "archive"), exist_ok=True)

    watermark = read_watermark(watermark_file)
    if watermark is None:
        # First run: anchor "now" and ingest nothing before today. Commit the
        # watermark immediately (there's no successful agent run to defer behind
        # on this benign first pass).
        os.makedirs(os.path.dirname(watermark_file) or ".", exist_ok=True)
        with open(watermark_file, "w", encoding="utf-8") as f:
            f.write(now_utc_iso())
        print(f"First run: watermark initialised at {watermark_file}. Starting from now.")
        sys.exit(20)

    token = get_access_token()
    ledger = load_ledger(ledger_file)
    folder_id = resolve_folder_id(token, inbox_folder)

    # List metadata only (no bodies), oldest-first from the watermark so a backlog
    # drains deterministically across nights. `ge` + ledger dedup means the
    # boundary message is re-listed but never re-filed.
    listing = graph_get(
        token,
        f"/me/mailFolders/{folder_id}/messages",
        params={
            "$top": max_per_run,
            "$orderby": "receivedDateTime asc",
            "$filter": f"receivedDateTime ge {watermark}",
            "$select": "id,internetMessageId,subject,from,receivedDateTime",
        },
    )
    messages = listing.get("value", [])

    staged = []
    for msg in messages:
        from_addr = (msg.get("from", {}) or {}).get("emailAddress", {}).get("address", "")
        if digest_from and from_addr.lower() == digest_from:
            continue
        if msg.get("internetMessageId") in ledger:
            continue
        staged.append(msg)

    if not staged:
        print("No new inbox email to stage.")
        sys.exit(20)

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
    # the newest staged message so the next run continues past this batch.
    pending_ids = os.path.join(staging_dir, "pending_ids.txt")
    with open(pending_ids, "w", encoding="utf-8") as f:
        for msg in staged:
            f.write(msg["internetMessageId"] + "\n")

    new_watermark = max(m.get("receivedDateTime", "") for m in staged) or watermark
    pending_watermark = os.path.join(staging_dir, "pending_watermark.txt")
    with open(pending_watermark, "w", encoding="utf-8") as f:
        f.write(new_watermark)

    print(f"Staged {len(staged)} inbox email(s) to {combined_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_inbox.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
