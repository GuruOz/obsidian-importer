#!/usr/bin/env python3
"""Timezone helpers so every timestamp the pipeline emits is in the app's local
zone (Singapore by default) regardless of how the container is configured.

Why this exists: the base image is python:3.12-slim and TZ is set to
Asia/Singapore in docker-compose, but if the OS tzdata package is missing,
glibc and naive datetime calls silently fall back to UTC. Routing every
local-time call through ZoneInfo here - and shipping the `tzdata` PyPI package
so the zone always resolves even without OS tzdata - makes correctness
independent of the container.

Convention:
* Microsoft Graph receivedDateTime values and the watermark files stay in UTC
  ("...Z"); Graph compares in UTC and lexical order of the Z-strings is
  chronological. Only *display* strings and *calendar-day* boundaries are local.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ_NAME = os.environ.get("TZ", "Asia/Singapore") or "Asia/Singapore"
try:
    APP_TZ = ZoneInfo(TZ_NAME)
except Exception:  # noqa: BLE001 - unknown zone name -> fall back to Singapore
    TZ_NAME = "Asia/Singapore"
    APP_TZ = ZoneInfo(TZ_NAME)


def now_local():
    """Current time as an aware datetime in the app timezone."""
    return datetime.now(APP_TZ)


def today_local():
    """Today's calendar date (YYYY-MM-DD) in the app timezone."""
    return now_local().strftime("%Y-%m-%d")


def _parse(value):
    """Coerce a datetime or ISO-8601 string (incl. Graph '...Z') to an aware
    datetime. Naive inputs are assumed to already be in the app timezone."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return dt


def to_local(value):
    """Convert a datetime / ISO string to an aware datetime in the app zone."""
    return _parse(value).astimezone(APP_TZ)


def fmt_local(value, fmt="%Y-%m-%d %H:%M"):
    """Format a datetime / ISO string as local wall-clock time."""
    return to_local(value).strftime(fmt)


def local_date_to_utc_iso(yyyy_mm_dd, end_exclusive=False):
    """Convert a local calendar date (YYYY-MM-DD) to the UTC instant of its
    midnight boundary, as a Graph-style '...Z' string. With end_exclusive, use
    the start of the *next* day (so an inclusive end date becomes an exclusive
    upper bound for a Graph `lt` filter)."""
    d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").replace(tzinfo=APP_TZ)
    if end_exclusive:
        d = d + timedelta(days=1)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
