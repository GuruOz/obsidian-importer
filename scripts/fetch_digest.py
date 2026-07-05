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
from datetime import datetime, timezone

from graph_mail import (
    env,
    get_access_token,
    graph_get,
    load_ledger,
    html_to_markdown,
    resolve_folder_id,
)


def message_to_markdown(message):
    body = message.get("body", {})
    content = body.get("content", "")
    content_type = body.get("contentType", "text")

    if content_type == "html":
        body_md = html_to_markdown(content)
    else:
        body_md = content.strip()

    received = message.get("receivedDateTime", "")
    return f"## Digest received {received}\n\n{body_md}\n"


def main():
    staging_dir = env("STAGING_DIR", "/work/staging", required=True)
    ledger_file = env("LEDGER_FILE", "/work/processed_ids.txt", required=True)
    digest_from = env("DIGEST_FROM", required=True)
    subject_pattern = env("DIGEST_SUBJECT_PATTERN", required=True)
    digest_folder = env("DIGEST_FOLDER", "Inbox")

    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "archive"), exist_ok=True)

    token = get_access_token()
    ledger = load_ledger(ledger_file)
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

    subject_re = re.compile(subject_pattern, re.IGNORECASE)
    matches = []
    for msg in messages:
        from_addr = (msg.get("from", {}) or {}).get("emailAddress", {}).get("address", "")
        subject = msg.get("subject", "") or ""
        if from_addr.lower() != digest_from.lower():
            continue
        if not subject_re.search(subject):
            continue
        if msg.get("internetMessageId") in ledger:
            continue
        matches.append(msg)

    if not matches:
        print("No new matching digest email found.")
        sys.exit(20)

    # Process all matches, newest last.
    matches.sort(key=lambda m: m.get("receivedDateTime", ""))

    for msg in matches:
        full = graph_get(token, f"/me/messages/{msg['id']}", params={"$select": "body"})
        msg["body"] = full.get("body", {})

    sections = [message_to_markdown(m) for m in matches]
    combined = "\n---\n\n".join(sections)

    digest_path = os.path.join(staging_dir, "digest.md")
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(combined)

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
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
