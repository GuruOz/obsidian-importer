#!/usr/bin/env python3
"""Interactive one-time Telegram login, creating the Telethon session file the
nightly fetch_telegram.py reads. Run it once from a terminal:

    docker compose exec -it pipeline python3 scripts/telegram_login.py

It prompts for your phone number, the login code Telegram sends you, and (if
enabled) your two-step-verification password. The dashboard Telegram page drives
the same steps without a terminal; this is the fallback / server path.

Reads TELEGRAM_API_ID / TELEGRAM_API_HASH (from .env) and writes the session to
TELEGRAM_SESSION_FILE (default /work/telegram/telegram.session).
"""
import os
import sys

from telethon.sync import TelegramClient


def main():
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        sys.exit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first "
                 "(get them from https://my.telegram.org).")

    session_file = os.environ.get("TELEGRAM_SESSION_FILE") or "/work/telegram/telegram.session"
    os.makedirs(os.path.dirname(session_file) or ".", exist_ok=True)

    with TelegramClient(session_file, api_id, api_hash) as client:
        # start() prompts for phone/code/password on the terminal as needed.
        client.start()
        me = client.get_me()
        name = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
        print(f"Logged in as {name or me.username or me.id}. Session saved to {session_file}.")


if __name__ == "__main__":
    main()
