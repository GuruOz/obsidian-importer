#!/usr/bin/env python3
"""Stage new Telegram messages (since a watermark, or over an explicit date
window) as one combined markdown file for the triage-and-file agent step.

Mirrors fetch_inbox.py's contract but for a Telegram user account via Telethon
(MTProto): the Bot API cannot read a person's own chats, so this uses a user
session (see scripts/telegram_login.py / the dashboard login page to create it).

Modes:
* Nightly watermark mode: fetch every message newer than the stored watermark
  across all included dialogs, globally sorted oldest-first and capped at
  TELEGRAM_MAX_PER_RUN (a backlog drains across nights). First run seeds the
  watermark at now - TELEGRAM_LOOKBACK_DAYS (default 0 = "start from now").
* Explicit window mode (TELEGRAM_START_DATE / TELEGRAM_END_DATE, YYYY-MM-DD
  Singapore days, end inclusive; START may be "all" for the whole history):
  a stateless backfill over that range. The watermark is bypassed and left
  untouched; the ledger alone records progress, so repeated passes drain the
  backlog TELEGRAM_MAX_PER_RUN at a time.

Include/exclude: TELEGRAM_INCLUDE_CHATS / TELEGRAM_EXCLUDE_CHATS (comma lists of
title / @username / numeric id, case-insensitive). Type toggles
TELEGRAM_INCLUDE_GROUPS (default on), _INCLUDE_CHANNELS (off), _INCLUDE_BOTS
(off); one-to-one chats are always eligible. A non-empty INCLUDE list is an
allowlist (explicit names override the type toggles). Every run rewrites
chats.json (all discovered dialogs) so the user can copy exact names.

Staging: one "chat-day" section per (chat, Singapore date), each carrying a
Section-ID line that doubles as the idempotency marker <!-- <section-id> -->.
pending_ids.txt holds one "tg:<chat_id>:<message_id>" per staged message.

Exit codes (consumed by run-ingest.sh):
    0   staged one or more messages
    1   unexpected/unhandled error
    20  nothing new to stage - benign skip
    30  Telegram session missing/unauthorized - needs a re-login
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from tzutil import APP_TZ, fmt_local, now_local, today_local

try:
    from telethon.sync import TelegramClient
    from telethon.errors import FloodWaitError
except Exception as exc:  # noqa: BLE001 - telethon missing/broken
    print(f"fetch_telegram: telethon import failed: {exc}", file=sys.stderr)
    sys.exit(1)


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required env var: {name}")
    return val


def _int_env(name, default):
    raw = (os.environ.get(name, str(default)) or str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}; using {default}.", file=sys.stderr)
        return default


def _bool_env(name, default):
    raw = (os.environ.get(name, "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _list_env(name):
    return [e.strip().lower() for e in (os.environ.get(name, "") or "").split(",") if e.strip()]


def read_watermark(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip() or None


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load_ledger(path):
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def parse_utc(iso):
    """Parse a stored watermark '%Y-%m-%dT%H:%M:%SZ' to an aware UTC datetime."""
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day_start_utc(yyyy_mm_dd):
    return datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").replace(tzinfo=APP_TZ).astimezone(timezone.utc)


# --- Dialog classification & filtering -------------------------------------

def dialog_type(dialog):
    if dialog.is_user:
        return "bot" if getattr(dialog.entity, "bot", False) else "dm"
    if dialog.is_group:            # small groups and megagroups
        return "group"
    if dialog.is_channel:          # broadcast channels
        return "channel"
    return "other"


def dialog_identifiers(dialog):
    ids = set()
    title = (dialog.name or "").strip().lower()
    if title:
        ids.add(title)
    username = getattr(dialog.entity, "username", None)
    if username:
        ids.add(username.lower())
        ids.add("@" + username.lower())
    ids.add(str(dialog.id))
    return ids


def dialog_included(dialog, include_list, exclude_list, type_toggles):
    ids = dialog_identifiers(dialog)
    if any(e in ids for e in exclude_list):
        return False
    if include_list:                       # allowlist: explicit names win over type
        return any(e in ids for e in include_list)
    dtype = dialog_type(dialog)
    if dtype == "dm":
        return True
    return type_toggles.get(dtype, False)


# --- Message rendering ------------------------------------------------------

def entity_name(ent):
    if ent is None:
        return "Unknown"
    fn = getattr(ent, "first_name", None)
    ln = getattr(ent, "last_name", None)
    if fn or ln:
        return f"{fn or ''} {ln or ''}".strip()
    title = getattr(ent, "title", None)
    if title:
        return title
    un = getattr(ent, "username", None)
    if un:
        return un
    return str(getattr(ent, "id", "Unknown"))


def sender_label(msg, client, cache):
    if msg.out:
        return "Me"
    sid = msg.sender_id
    if sid is None:
        return "Unknown"
    if sid in cache:
        return cache[sid]
    ent = msg.sender
    if ent is None:
        try:
            ent = client.get_entity(sid)
        except Exception:  # noqa: BLE001 - inaccessible user, fall back to id
            ent = None
    name = entity_name(ent) if ent is not None else str(sid)
    cache[sid] = name
    return name


def message_text(msg):
    """Human-readable text for a message, or None to skip (service messages)."""
    if msg.message:
        return msg.message.strip()
    if msg.media:
        if getattr(msg, "photo", None):
            return "[photo]"
        if getattr(msg, "video", None):
            return "[video]"
        if getattr(msg, "voice", None):
            return "[voice message]"
        if getattr(msg, "audio", None):
            return "[audio]"
        if getattr(msg, "sticker", None):
            emoji = getattr(msg.file, "emoji", "") if getattr(msg, "file", None) else ""
            return f"[sticker {emoji}]".strip()
        if getattr(msg, "document", None):
            fname = getattr(msg.file, "name", None) if getattr(msg, "file", None) else None
            return f"[document: {fname}]" if fname else "[document]"
        if getattr(msg, "geo", None):
            return "[location]"
        if getattr(msg, "poll", None):
            return "[poll]"
        if getattr(msg, "contact", None):
            return "[contact]"
        return "[media]"
    return None  # service message (join/leave/pin/etc.)


def build_record(msg, dialog, client, sender_cache):
    text = message_text(msg)
    if text is None:
        return None
    local_dt = msg.date.astimezone(APP_TZ)
    return {
        "uid": f"tg:{dialog.id}:{msg.id}",
        "chat_id": dialog.id,
        "chat_name": dialog.name or str(dialog.id),
        "type": dialog_type(dialog),
        "date_utc": msg.date,
        "sgt_date": local_dt.strftime("%Y-%m-%d"),
        "sgt_time": local_dt.strftime("%H:%M"),
        "sender": sender_label(msg, client, sender_cache),
        "text": text,
    }


def flood_guarded(fn):
    """Run a Telethon call, sleeping through short flood-waits and failing hard
    on long ones (a >60s wait means we're rate-limited enough to back off)."""
    try:
        return fn()
    except FloodWaitError as e:
        if e.seconds and e.seconds <= 60:
            import time
            print(f"fetch_telegram: flood-wait {e.seconds}s; sleeping.", file=sys.stderr)
            time.sleep(e.seconds + 1)
            return fn()
        print(f"fetch_telegram: flood-wait {getattr(e, 'seconds', '?')}s exceeds 60s; "
              "backing off (will retry next run).", file=sys.stderr)
        sys.exit(1)


# --- Collection strategies --------------------------------------------------

def collect_watermark(client, dialogs, watermark_dt, ledger, max_per_run, sender_cache):
    """Nightly mode: examine every message after the watermark across all
    included dialogs, globally sort oldest-first, and take up to max_per_run.

    Returns (staged_records, examined_max_dt). examined_max_dt is the newest
    message date seen at all (staged, ledgered, or content-less), so an
    all-skipped window can still advance the watermark past it.
    """
    candidates = []
    examined_max = watermark_dt
    for dialog in dialogs:
        def _iter():
            return list(client.iter_messages(dialog.entity, offset_date=watermark_dt, reverse=True))
        for msg in flood_guarded(_iter):
            if msg.date <= watermark_dt:
                continue
            if msg.date > examined_max:
                examined_max = msg.date
            uid = f"tg:{dialog.id}:{msg.id}"
            if uid in ledger:
                continue
            rec = build_record(msg, dialog, client, sender_cache)
            if rec is not None:
                candidates.append(rec)
    candidates.sort(key=lambda r: (r["date_utc"], r["chat_id"]))
    return candidates[:max_per_run], examined_max


def collect_window(client, dialogs, start_dt, end_dt, ledger, max_per_run, sender_cache):
    """Explicit-window mode: sequential per-dialog scan, capped at max_per_run
    (finishing the in-progress chat-day so sections stay whole). No watermark;
    the ledger drains the backlog across passes."""
    staged = []
    reached_cap = False
    for dialog in dialogs:
        if reached_cap:
            break
        cur_key = None

        def _iter():
            return list(client.iter_messages(dialog.entity, offset_date=start_dt, reverse=True))
        for msg in flood_guarded(_iter):
            if end_dt is not None and msg.date >= end_dt:
                break
            if start_dt is not None and msg.date < start_dt:
                continue
            uid = f"tg:{dialog.id}:{msg.id}"
            if uid in ledger:
                continue
            rec = build_record(msg, dialog, client, sender_cache)
            if rec is None:
                continue
            key = (rec["chat_id"], rec["sgt_date"])
            if len(staged) >= max_per_run and key != cur_key:
                reached_cap = True
                break
            staged.append(rec)
            cur_key = key
    return staged


# --- Section assembly -------------------------------------------------------

def render_sections(records):
    """Group records into (chat, sgt_date) sections, ordered by date then chat."""
    buckets = {}
    for r in records:
        buckets.setdefault((r["sgt_date"], r["chat_id"]), []).append(r)
    sections = []
    for (sgt_date, chat_id) in sorted(buckets.keys()):
        msgs = sorted(buckets[(sgt_date, chat_id)], key=lambda r: (r["date_utc"]))
        head = msgs[0]
        lines = [
            f"## Chat: {head['chat_name']} — {sgt_date}",
            f"Section-ID: telegram:{chat_id}:{sgt_date}",
            f"Type: {head['type']}",
            "",
        ]
        for m in msgs:
            lines.append(f"- [{m['sgt_time']}] {m['sender']}: {m['text']}")
        sections.append("\n".join(lines))
    return sections


def write_chats_json(dialogs, out_dir):
    import json
    rows = []
    for d in dialogs:
        rows.append({
            "id": d.id,
            "title": d.name or "",
            "username": getattr(d.entity, "username", None),
            "type": dialog_type(d),
        })
    write_text(os.path.join(out_dir, "chats.json"), json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    api_id = _int_env("TELEGRAM_API_ID", 0)
    api_hash = env("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        print("fetch_telegram: TELEGRAM_API_ID / TELEGRAM_API_HASH not set "
              "(get them from https://my.telegram.org).", file=sys.stderr)
        sys.exit(30)

    session_file = (
        env("TELEGRAM_SESSION_FILE")
        or "/work/telegram/telegram.session"
    )
    staging_dir = env("STAGING_DIR", "/work/staging/telegram", required=True)
    ledger_file = env("LEDGER_FILE", "/work/telegram_processed_ids.txt", required=True)
    watermark_file = (
        os.environ.get("WATERMARK_FILE")
        or env("TELEGRAM_WATERMARK_FILE", "/work/telegram_watermark.txt")
    )
    max_per_run = _int_env("TELEGRAM_MAX_PER_RUN", 500)
    lookback_days = max(_int_env("TELEGRAM_LOOKBACK_DAYS", 0), 0)
    # Per-source switch wins over the exported global (see fetch_whatsapp.py).
    dry_run = (os.environ.get("TELEGRAM_DRY_RUN") or os.environ.get("DRY_RUN") or "1") == "1"

    include_list = _list_env("TELEGRAM_INCLUDE_CHATS")
    exclude_list = _list_env("TELEGRAM_EXCLUDE_CHATS")
    type_toggles = {
        "group": _bool_env("TELEGRAM_INCLUDE_GROUPS", True),
        "channel": _bool_env("TELEGRAM_INCLUDE_CHANNELS", False),
        "bot": _bool_env("TELEGRAM_INCLUDE_BOTS", False),
    }

    # Window mode?
    start_raw = (env("TELEGRAM_START_DATE", "") or "").strip()
    end_raw = (env("TELEGRAM_END_DATE", "") or "").strip()
    window_mode = bool(start_raw)
    start_dt = end_dt = None
    if window_mode:
        if start_raw.lower() == "all":
            start_dt = datetime(2013, 8, 1, tzinfo=timezone.utc)  # before Telegram existed
        else:
            try:
                start_dt = local_day_start_utc(start_raw)
            except ValueError:
                sys.exit(f"TELEGRAM_START_DATE must be YYYY-MM-DD or 'all', got {start_raw!r}")
        if end_raw:
            try:
                end_dt = local_day_start_utc(end_raw) + timedelta(days=1)  # inclusive
            except ValueError:
                sys.exit(f"TELEGRAM_END_DATE must be YYYY-MM-DD, got {end_raw!r}")
    elif end_raw:
        sys.exit("TELEGRAM_END_DATE requires TELEGRAM_START_DATE to be set too.")

    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "archive"), exist_ok=True)
    os.makedirs(os.path.dirname(session_file) or ".", exist_ok=True)

    print(f"fetch_telegram: max_per_run={max_per_run} lookback_days={lookback_days} "
          f"dry_run={dry_run} window={'yes' if window_mode else 'no'} "
          f"include={include_list or '-'} exclude={exclude_list or '-'} "
          f"toggles={type_toggles} staging={staging_dir}")

    client = TelegramClient(session_file, api_id, api_hash)
    client.connect()
    if not client.is_user_authorized():
        print("fetch_telegram: session missing or unauthorized - log in first "
              "(dashboard Telegram page or scripts/telegram_login.py).", file=sys.stderr)
        client.disconnect()
        sys.exit(30)

    try:
        ledger = load_ledger(ledger_file)
        print(f"fetch_telegram: ledger holds {len(ledger)} already-processed message id(s)")

        all_dialogs = list(flood_guarded(lambda: list(client.iter_dialogs())))
        # chats.json lists EVERY dialog (discovery), regardless of the filter, in
        # the telegram data dir (alongside the session) so the dashboard can read
        # it from /host/data/telegram/chats.json.
        write_chats_json(all_dialogs, os.path.dirname(session_file) or staging_dir)

        included = [d for d in all_dialogs
                    if dialog_included(d, include_list, exclude_list, type_toggles)]
        included.sort(key=lambda d: d.id)  # stable order for window-mode drain
        print(f"fetch_telegram: {len(included)}/{len(all_dialogs)} dialog(s) pass the filter")

        sender_cache = {}
        examined_max = None
        if window_mode:
            staged = collect_window(client, included, start_dt, end_dt, ledger,
                                    max_per_run, sender_cache)
            watermark = None
        else:
            watermark_iso = read_watermark(watermark_file)
            if watermark_iso is None:
                seed = now_local().astimezone(timezone.utc) - timedelta(days=lookback_days)
                watermark_dt = seed
                if not dry_run:
                    write_text(watermark_file, fmt_utc(seed))
                print(f"fetch_telegram: first run, watermark seeded at {fmt_utc(seed)} "
                      f"({lookback_days} day(s) back)"
                      f"{' (dry run: not persisted)' if dry_run else ''}.")
            else:
                watermark_dt = parse_utc(watermark_iso)
                print(f"fetch_telegram: watermark is {watermark_iso}")
            staged, examined_max = collect_watermark(client, included, watermark_dt,
                                                     ledger, max_per_run, sender_cache)
            watermark = watermark_dt
    finally:
        client.disconnect()

    if not staged:
        # Nightly mode with everything examined skipped/ledgered: advance the
        # watermark past it (written directly, since run-ingest exits on 20
        # before its deferred commit) so the same window isn't re-scanned nightly.
        if (not window_mode and not dry_run and examined_max is not None
                and watermark is not None and examined_max > watermark):
            write_text(watermark_file, fmt_utc(examined_max))
            print(f"fetch_telegram: watermark advanced to {fmt_utc(examined_max)} "
                  "(all listed messages skipped).")
        print("No new Telegram messages to stage.")
        sys.exit(20)

    sections = render_sections(staged)
    n_chats = len({r["chat_id"] for r in staged})
    header = (
        f"# Telegram batch — fetched {now_local():%Y-%m-%d %H:%M} SGT\n\n"
        f"{len(staged)} message(s) in {len(sections)} chat-day section(s) "
        f"across {n_chats} chat(s).\n\n---\n\n"
    )
    combined = header + "\n\n---\n\n".join(sections) + "\n"

    combined_path = os.path.join(staging_dir, "telegram.md")
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(combined)

    archive_path = os.path.join(staging_dir, "archive", f"{today_local()}.md")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(combined)

    pending_ids = os.path.join(staging_dir, "pending_ids.txt")
    with open(pending_ids, "w", encoding="utf-8") as f:
        for r in staged:
            f.write(r["uid"] + "\n")

    pending_watermark = os.path.join(staging_dir, "pending_watermark.txt")
    if not window_mode:
        newest = max(r["date_utc"] for r in staged)
        with open(pending_watermark, "w", encoding="utf-8") as f:
            f.write(fmt_utc(newest))
    elif os.path.exists(pending_watermark):
        os.remove(pending_watermark)  # window mode never advances the watermark

    print(f"Staged {len(staged)} Telegram message(s) in {len(sections)} section(s) "
          f"to {combined_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_telegram.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
