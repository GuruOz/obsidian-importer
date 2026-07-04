#!/usr/bin/env python3
"""Pull the nightly M365 Copilot digest email via Microsoft Graph, convert it to
markdown, and stage it for the agent filing step.

Exit codes (consumed by run-digest.sh):
    0   staged a digest successfully
    1   unexpected/unhandled error
    20  no new matching email found - orchestrator should skip the agent and notify benignly
    30  Graph auth failed (token cache missing/expired) - needs `graph_auth.py` re-run
"""
import os
import re
import stat
import sys
from datetime import datetime, timezone

import msal
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required env var: {name}")
    return val


def get_access_token():
    client_id = env("GRAPH_CLIENT_ID", required=True)
    tenant = env("GRAPH_TENANT", "consumers")
    cache_file = env("MSAL_CACHE_FILE", "/work/msal_cache.json")

    cache = msal.SerializableTokenCache()
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cache.deserialize(f.read())
    else:
        print("No token cache found. Run graph_auth.py once interactively first.", file=sys.stderr)
        sys.exit(30)

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if not accounts:
        print("No cached account. Run graph_auth.py once interactively first.", file=sys.stderr)
        sys.exit(30)

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        print(f"Silent token acquisition failed: {result}", file=sys.stderr)
        sys.exit(30)

    if cache.has_state_changed:
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(cache.serialize())
        os.chmod(cache_file, stat.S_IRUSR | stat.S_IWUSR)

    return result["access_token"]


def graph_get(token, path, params=None):
    resp = requests.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def load_ledger(ledger_file):
    if not os.path.exists(ledger_file):
        return set()
    with open(ledger_file, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def append_ledger(ledger_file, message_id):
    with open(ledger_file, "a", encoding="utf-8") as f:
        f.write(message_id + "\n")


def html_to_markdown(html):
    soup = BeautifulSoup(html, "html.parser")

    # Strip tracking pixels and other 1x1 images.
    for img in soup.find_all("img"):
        width = str(img.get("width", "")).strip()
        height = str(img.get("height", "")).strip()
        if width in ("0", "1") or height in ("0", "1"):
            img.decompose()

    md = markdownify(str(soup), heading_style="ATX")

    # Collapse empty headings and excess blank lines left by boilerplate footers.
    md = re.sub(r"^#+\s*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


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


def resolve_folder_id(token, folder_path):
    """Resolve a mail folder path like 'Inbox/CopilotDigest' or 'CopilotDigest' to its id.

    Read-only: the folder must already exist (created by the user's Outlook rule).
    A leading 'Inbox' segment uses Graph's locale-independent well-known folder name.
    """
    parts = [p for p in folder_path.split("/") if p]

    parent_id = None
    if parts and parts[0].lower() == "inbox":
        parent_id = "inbox"
        parts = parts[1:]
    if parent_id and not parts:
        return parent_id

    for part in parts:
        list_path = f"/me/mailFolders/{parent_id}/childFolders" if parent_id else "/me/mailFolders"
        found = graph_get(token, list_path, params={"$filter": f"displayName eq '{part}'"})
        values = found.get("value", [])
        if not values:
            sys.exit(
                f"Mail folder '{folder_path}' not found (missing segment: '{part}'). "
                "Create it (e.g. via your Outlook rule) or fix DIGEST_FOLDER in .env."
            )
        parent_id = values[0]["id"]
    return parent_id


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

    for msg in matches:
        append_ledger(ledger_file, msg["internetMessageId"])

    print(f"Staged {len(matches)} digest email(s) to {digest_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_digest.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
