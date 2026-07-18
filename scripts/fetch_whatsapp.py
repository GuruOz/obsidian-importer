#!/usr/bin/env python3
"""Stage new WhatsApp messages as one combined markdown file for the
triage-and-file agent step.

Unlike the email/Telegram fetchers, this one does NO network I/O: the always-on
Baileys bridge (see bridge/) maintains the WhatsApp companion-device session and
appends every message - history-sync batches and live - to a per-day JSONL store
under /work/whatsapp/messages/. This fetcher just reads that store, filters it,
and stages it, so it fits the same run-once-nightly shape as the other sources.

Modes (same semantics as fetch_telegram.py):
* Nightly watermark mode: stage messages newer than the stored watermark, capped
  at WHATSAPP_MAX_PER_RUN. First run seeds the watermark at now -
  WHATSAPP_LOOKBACK_DAYS (default 0 = start from now).
* Explicit window mode (WHATSAPP_START_DATE / WHATSAPP_END_DATE, YYYY-MM-DD
  Singapore days, end inclusive): a stateless backfill over that range - the
  watermark is bypassed; the ledger drains the backlog across passes.

Include/exclude: WHATSAPP_INCLUDE_CHATS / WHATSAPP_EXCLUDE_CHATS (comma lists of
chat name or JID, case-insensitive; a non-empty INCLUDE is an allowlist).
WHATSAPP_INCLUDE_GROUPS (default on) toggles group chats; one-to-one chats are
always eligible.

Staging: one "chat-day" section per (chat, Singapore date), each with a
Section-ID line that doubles as the marker <!-- <section-id> -->. pending_ids.txt
holds one "wa:<chat_jid>:<message_id>" per staged message.

Exit codes (consumed by run-ingest.sh):
    0   staged one or more messages
    1   unexpected/unhandled error
    20  nothing new to stage - benign skip
    30  bridge not paired (status missing or logged_out) - re-pair from dashboard
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from tzutil import APP_TZ, now_local, today_local


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


def read_text(path):
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
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ts_to_utc_iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_sgt(ts):
    return datetime.fromtimestamp(ts, APP_TZ)


def chat_included(rec, include_list, exclude_list, include_groups):
    name = (rec.get("chat_name") or "").strip().lower()
    jid = (rec.get("chat_jid") or "").strip().lower()
    ids = {name, jid}
    if any(e in ids for e in exclude_list):
        return False
    if include_list:                       # allowlist: names override the group toggle
        return any(e in ids for e in include_list)
    if rec.get("is_group"):
        return include_groups
    return True                             # one-to-one chats always eligible


def iter_store_records(msg_dir, date_from=None, date_to=None):
    """Yield (sgt_date, record) from the bridge JSONL store, in ascending date
    order. date_from/date_to (YYYY-MM-DD SGT) bound which day-files are read."""
    for path in sorted(glob.glob(os.path.join(msg_dir, "*.jsonl"))):
        day = os.path.basename(path)[:-6]  # strip ".jsonl"
        if len(day) != 10:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    yield day, rec
        except OSError:
            continue


def render_sections(records):
    """Group records into (chat, sgt_date) sections, ordered by date then chat."""
    buckets = {}
    for r in records:
        buckets.setdefault((r["_sgt_date"], r["chat_jid"]), []).append(r)
    sections = []
    for key in sorted(buckets.keys()):
        msgs = sorted(buckets[key], key=lambda r: r["ts"])
        head = msgs[0]
        sgt_date, chat_jid = key
        lines = [
            f"## Chat: {head.get('chat_name') or chat_jid} — {sgt_date}",
            f"Section-ID: whatsapp:{chat_jid}:{sgt_date}",
            f"Type: {'group' if head.get('is_group') else 'dm'}",
            "",
        ]
        for m in msgs:
            t = ts_to_sgt(m["ts"]).strftime("%H:%M")
            sender = "Me" if m.get("from_me") else (m.get("sender_name") or "Unknown")
            lines.append(f"- [{t}] {sender}: {m.get('text', '')}")
        sections.append("\n".join(lines))
    return sections


def main():
    data_dir = env("WHATSAPP_DATA_DIR", "/work/whatsapp")
    msg_dir = os.path.join(data_dir, "messages")
    status_file = os.path.join(data_dir, "status.json")

    staging_dir = env("STAGING_DIR", "/work/staging/whatsapp", required=True)
    ledger_file = env("LEDGER_FILE", "/work/whatsapp_processed_ids.txt", required=True)
    watermark_file = (
        os.environ.get("WATERMARK_FILE")
        or env("WHATSAPP_WATERMARK_FILE", "/work/whatsapp_watermark.txt")
    )
    max_per_run = _int_env("WHATSAPP_MAX_PER_RUN", 500)
    lookback_days = max(_int_env("WHATSAPP_LOOKBACK_DAYS", 0), 0)
    # Per-source switch wins over the exported global, so a live digest
    # (DRY_RUN=0 in creation env) can never drag a standalone fetcher run live
    # when WHATSAPP_DRY_RUN says dry. Identical under run-ingest.sh, which
    # exports DRY_RUN already resolved from WHATSAPP_DRY_RUN.
    dry_run = (os.environ.get("WHATSAPP_DRY_RUN") or os.environ.get("DRY_RUN") or "1") == "1"
    include_list = _list_env("WHATSAPP_INCLUDE_CHATS")
    exclude_list = _list_env("WHATSAPP_EXCLUDE_CHATS")
    include_groups = _bool_env("WHATSAPP_INCLUDE_GROUPS", True)

    # The bridge must be paired for there to be anything to read.
    status = {}
    raw_status = read_text(status_file)
    if raw_status:
        try:
            status = json.loads(raw_status)
        except ValueError:
            status = {}
    if not raw_status:
        print("fetch_whatsapp: bridge status.json not found - is the whatsapp-bridge "
              "container running and paired?", file=sys.stderr)
        sys.exit(30)
    if status.get("state") == "logged_out":
        print("fetch_whatsapp: bridge is logged out - re-pair from the dashboard "
              "Connections page.", file=sys.stderr)
        sys.exit(30)

    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "archive"), exist_ok=True)

    start_raw = (env("WHATSAPP_START_DATE", "") or "").strip()
    end_raw = (env("WHATSAPP_END_DATE", "") or "").strip()
    window_mode = bool(start_raw)
    if not window_mode and end_raw:
        sys.exit("WHATSAPP_END_DATE requires WHATSAPP_START_DATE to be set too.")
    for label, val in (("WHATSAPP_START_DATE", start_raw), ("WHATSAPP_END_DATE", end_raw)):
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                sys.exit(f"{label} must be YYYY-MM-DD, got {val!r}")
    if window_mode and end_raw and end_raw < start_raw:
        sys.exit(f"WHATSAPP_END_DATE ({end_raw}) is before WHATSAPP_START_DATE ({start_raw}).")

    print(f"fetch_whatsapp: max_per_run={max_per_run} lookback_days={lookback_days} "
          f"dry_run={dry_run} window={'yes' if window_mode else 'no'} "
          f"include={include_list or '-'} exclude={exclude_list or '-'} "
          f"include_groups={include_groups} store={msg_dir}")

    ledger = load_ledger(ledger_file)
    print(f"fetch_whatsapp: ledger holds {len(ledger)} already-processed message id(s)")

    # Resolve the read range.
    watermark_ts = None
    date_from = date_to = None
    if window_mode:
        date_from, date_to = start_raw, (end_raw or None)
    else:
        wm = read_text(watermark_file)
        if wm is None:
            seed = now_local().astimezone(timezone.utc) - timedelta(days=lookback_days)
            watermark_ts = seed.timestamp()
            if not dry_run:
                write_text(watermark_file, ts_to_utc_iso(int(watermark_ts)))
            print(f"fetch_whatsapp: first run, watermark seeded at {ts_to_utc_iso(int(watermark_ts))} "
                  f"({lookback_days} day(s) back)"
                  f"{' (dry run: not persisted)' if dry_run else ''}.")
        else:
            watermark_ts = parse_utc(wm).timestamp()
            print(f"fetch_whatsapp: watermark is {wm}")
        # Start reading from the watermark's SGT day (records are then ts-filtered).
        date_from = ts_to_sgt(watermark_ts).strftime("%Y-%m-%d")

    staged = []
    examined_max_ts = watermark_ts if watermark_ts is not None else 0
    reached_cap = False
    cur_key = None
    for sgt_date, rec in iter_store_records(msg_dir, date_from, date_to):
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if not window_mode and watermark_ts is not None and ts <= watermark_ts:
            continue
        if ts > examined_max_ts:
            examined_max_ts = ts
        if not chat_included(rec, include_list, exclude_list, include_groups):
            continue
        uid = f"wa:{rec.get('chat_jid')}:{rec.get('id')}"
        if uid in ledger:
            continue
        if not (rec.get("text") or "").strip():
            continue
        rec["_sgt_date"] = sgt_date
        rec["_uid"] = uid
        key = (rec.get("chat_jid"), sgt_date)
        if len(staged) >= max_per_run and key != cur_key:
            reached_cap = True
            break
        staged.append(rec)
        cur_key = key

    if not staged:
        if (not window_mode and not dry_run and watermark_ts is not None
                and examined_max_ts > watermark_ts):
            write_text(watermark_file, ts_to_utc_iso(int(examined_max_ts)))
            print(f"fetch_whatsapp: watermark advanced to {ts_to_utc_iso(int(examined_max_ts))} "
                  "(all listed messages skipped).")
        print("No new WhatsApp messages to stage.")
        sys.exit(20)

    sections = render_sections(staged)
    n_chats = len({r["chat_jid"] for r in staged})
    header = (
        f"# WhatsApp batch — fetched {now_local():%Y-%m-%d %H:%M} SGT\n\n"
        f"{len(staged)} message(s) in {len(sections)} chat-day section(s) "
        f"across {n_chats} chat(s).{' [capped]' if reached_cap else ''}\n\n---\n\n"
    )
    combined = header + "\n\n---\n\n".join(sections) + "\n"

    combined_path = os.path.join(staging_dir, "whatsapp.md")
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(combined)

    archive_path = os.path.join(staging_dir, "archive", f"{today_local()}.md")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(combined)

    pending_ids = os.path.join(staging_dir, "pending_ids.txt")
    with open(pending_ids, "w", encoding="utf-8") as f:
        for r in staged:
            f.write(r["_uid"] + "\n")

    pending_watermark = os.path.join(staging_dir, "pending_watermark.txt")
    if not window_mode:
        newest = max(r["ts"] for r in staged)
        with open(pending_watermark, "w", encoding="utf-8") as f:
            f.write(ts_to_utc_iso(int(newest)))
    elif os.path.exists(pending_watermark):
        os.remove(pending_watermark)

    print(f"Staged {len(staged)} WhatsApp message(s) in {len(sections)} section(s) "
          f"to {combined_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"fetch_whatsapp.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
