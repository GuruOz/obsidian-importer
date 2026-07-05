#!/usr/bin/env python3
"""Shared Microsoft Graph mail helpers.

Extracted verbatim from the original fetch_digest.py so every ingestion source
(the Copilot digest, the personal-inbox triage, and any future mail source)
uses one code path for token acquisition, Graph reads, HTML->markdown
conversion, ledger dedup, and mail-folder resolution. Strictly read-only:
nothing here writes to the mailbox.

Exit codes raised from here (consumed by the orchestrator via the fetcher's
exit status):
    30  Graph auth failed (token cache missing/expired) - needs graph_auth.py re-run
Callers add their own 0 / 20 / 1 semantics on top.
"""
import os
import re
import stat
import sys

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
        # OData string literals escape single quotes by doubling them.
        escaped = part.replace("'", "''")
        found = graph_get(token, list_path, params={"$filter": f"displayName eq '{escaped}'"})
        values = found.get("value", [])
        if not values:
            sys.exit(
                f"Mail folder '{folder_path}' not found (missing segment: '{part}'). "
                "Create it (e.g. via your Outlook rule) or fix DIGEST_FOLDER in .env."
            )
        parent_id = values[0]["id"]
    return parent_id
