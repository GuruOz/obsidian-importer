#!/usr/bin/env python3
"""Pull the nightly M365 Copilot digest email via Microsoft Graph, convert it to
markdown, and stage it for the agent filing step.

Exit codes (consumed by run-ingest.sh):
    0   staged a digest successfully
    1   unexpected/unhandled error
    20  no new matching email found - orchestrator should skip the agent and notify benignly
    30  Graph auth failed (token cache missing/expired) - needs `graph_auth.py` re-run
"""
import os
import re
import sys

from graph_mail import (
    env,
    get_access_token,
    graph_get,
    load_ledger,
    html_to_markdown,
    resolve_folder_id,
)
from tzutil import fmt_local, today_local


def message_to_markdown(message, kind="digest"):
    body = message.get("body", {})
    content = body.get("content", "")
    content_type = body.get("contentType", "text")

    if content_type == "html":
        body_md = html_to_markdown(content)
    else:
        body_md = content.strip()

    received_raw = message.get("receivedDateTime", "")
    # Graph returns UTC; show Singapore wall-clock so the agent files under the
    # correct local day. (verify_filing.py derives digest dates from the email
    # body's "Work Log - DD Mon YYYY" lines, not this header, so changing the
    # displayed format here is safe.)
    received = f"{fmt_local(received_raw)} SGT" if received_raw else ""
    if kind == "note":
        # Ad-hoc work notes are idempotency-keyed per message (a date's digest
        # marker must not block them), so the agent needs the Message-ID; the
        # subject gives it filing context a free-form body may lack.
        subject = message.get("subject", "") or ""
        mid = message.get("internetMessageId", "") or ""
        return (f"## Ad-hoc note received {received}\n"
                f"Subject: {subject}\n"
                f"Message-ID: {mid}\n\n{body_md}\n")
    return f"## Digest received {received}\n\n{body_md}\n"


def main():
    staging_dir = env("STAGING_DIR", "/work/staging", required=True)
    ledger_file = env("LEDGER_FILE", "/work/processed_ids.txt", required=True)
    digest_from = env("DIGEST_FROM", required=True)
    subject_pattern = env("DIGEST_SUBJECT_PATTERN", required=True)
    # Second subject class from the same sender: ad-hoc work notes the user
    # emails themselves. Blank disables them.
    note_pattern = env("DIGEST_NOTE_SUBJECT_PATTERN", "obsidian note")
    digest_folder = env("DIGEST_FOLDER", "Inbox")

    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "archive"), exist_ok=True)

    print(f"fetch_digest: folder={digest_folder!r} from={digest_from!r} "
          f"subject_pattern={subject_pattern!r} note_pattern={note_pattern!r} "
          f"staging={staging_dir}")
    token = get_access_token()
    ledger = load_ledger(ledger_file)
    print(f"fetch_digest: ledger holds {len(ledger)} already-processed message id(s)")
    folder_id = resolve_folder_id(token, digest_folder)

    # Two-phase fetch: list metadata only (no bodies), then download bodies just for
    # the (usually one) message that actually matches - avoids pulling 50 full HTML
    # bodies over the wire every night.
    listing = graph_get(
        token,
        f"/me/mailFolders/{folder_id}/messages",
        params={
            "$top": 50,
            "$orderby": "receivedDateTime desc",
            "$select": "id,internetMessageId,subject,from,receivedDateTime",
        },
    )
    messages = listing.get("value", [])

    print(f"fetch_digest: listed {len(messages)} most-recent message(s) in folder")
    subject_re = re.compile(subject_pattern, re.IGNORECASE)
    note_re = re.compile(note_pattern, re.IGNORECASE) if note_pattern else None
    matches = []
    for msg in messages:
        from_addr = (msg.get("from", {}) or {}).get("emailAddress", {}).get("address", "")
        subject = msg.get("subject", "") or ""
        received = msg.get("receivedDateTime", "") or ""
        if from_addr.lower() != digest_from.lower():
            print(f"fetch_digest:   skip (wrong sender {from_addr}) [{received}] {subject!r}")
            continue
        if subject_re.search(subject):
            kind = "digest"
        elif note_re and note_re.search(subject):
            kind = "note"
        else:
            print(f"fetch_digest:   skip (subject mismatch) [{received}] {subject!r}")
            continue
        if msg.get("internetMessageId") in ledger:
            print(f"fetch_digest:   skip (already processed) [{received}] {subject!r}")
            continue
        print(f"fetch_digest:   match ({kind}) [{received}] {subject!r}")
        msg["_kind"] = kind
        matches.append(msg)

    if not matches:
        print("No new matching digest email found.")
        sys.exit(20)

    # Process all matches, newest last.
    matches.sort(key=lambda m: m.get("receivedDateTime", ""))

    for msg in matches:
        full = graph_get(token, f"/me/messages/{msg['id']}", params={"$select": "body"})
        msg["body"] = full.get("body", {})

    sections = [message_to_markdown(m, m.get("_kind", "digest")) for m in matches]
    combined = "\n---\n\n".join(sections)

    digest_path = os.path.join(staging_dir, "digest.md")
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(combined)

    today = today_local()
    archive_path = os.path.join(staging_dir, "archive", f"{today}.md")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(combined)

    # Ledger commitment is deferred: the IDs are staged here, and run-ingest.sh appends
    # them to the ledger only after the agent step succeeds. A failed filing run
    # therefore leaves these emails un-ledgered, and the next night's fetch picks them
    # up again automatically instead of losing that day's digest forever.
    pending_path = os.path.join(staging_dir, "pending_ids.txt")
    with open(pending_path, "w", encoding="utf-8") as f:
        for msg in matches:
            f.write(msg["internetMessageId"] + "\n")

    print(f"Staged {len(matches)} digest email(s) to {digest_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_digest.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
