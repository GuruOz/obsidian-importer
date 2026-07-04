#!/usr/bin/env python3
"""One-time interactive bootstrap: acquire a Microsoft Graph refresh token via the
device-code flow and persist it to MSAL_CACHE_FILE so fetch_digest.py can acquire
access tokens silently on every nightly run.

Run once, interactively:
    docker compose run --rm pipeline python3 scripts/graph_auth.py
"""
import os
import stat
import sys

import msal

SCOPES = ["Mail.Read"]


def main():
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    tenant = os.environ.get("GRAPH_TENANT", "consumers")
    cache_file = os.environ.get("MSAL_CACHE_FILE", "/work/msal_cache.json")

    if not client_id:
        sys.exit("GRAPH_CLIENT_ID is not set. Fill it in .env first (see README).")

    cache = msal.SerializableTokenCache()
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )

    def save_cache():
        if cache.has_state_changed:
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(cache.serialize())
            os.chmod(cache_file, stat.S_IRUSR | stat.S_IWUSR)

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_cache()
            print(f"Already authenticated as {accounts[0].get('username')}. Nothing to do.")
            return

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        sys.exit(f"Failed to start device flow: {flow}")

    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        sys.exit(f"Authentication failed: {result.get('error_description', result)}")

    save_cache()
    print(f"Authenticated. Token cache written to {cache_file}.")


if __name__ == "__main__":
    main()
