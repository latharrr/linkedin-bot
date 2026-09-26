"""IST time helpers. Everything is stored as UTC-aware datetimes; IST is only
used to decide *which* instant a human-facing time ("9 AM", "18:30") means."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_HHMM = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("naive datetime; pass a timezone-aware value")


def next_ist_occurrence(hour: int, minute: int, now: datetime) -> datetime:
    """Next instant strictly after `now` whose IST wall clock reads hour:minute (returned in UTC)."""
    _require_aware(now)
    local_now = now.astimezone(IST)
    candidate = datetime.combine(local_now.date(), time(hour, minute), tzinfo=IST)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), time(hour, minute), tzinfo=IST)
    return candidate.astimezone(UTC)


def next_9am(now: datetime) -> datetime:
    return next_ist_occurrence(9, 0, now)


def next_6pm(now: datetime) -> datetime:
    return next_ist_occurrence(18, 0, now)


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """'18:30', '9:05', '09.05' -> (h, m). Anything else -> None."""
    m = _HHMM.match(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def custom_time(text: str, now: datetime) -> datetime | None:
    """SPEC §5.2 save_custom_time: today IST at HH:MM; if already past, roll to tomorrow."""
    parsed = parse_hhmm(text)
    if parsed is None:
        return None
    return next_ist_occurrence(parsed[0], parsed[1], now)


_AMPM = re.compile(r"^\s*(1[0-2]|0?[1-9])(?:[:.]([0-5]\d))?\s*([ap])\.?m\.?\s*$", re.IGNORECASE)
_DATED = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T]+(.+)$")
MAX_AHEAD = timedelta(days=60)


def parse_when(text: str, now: datetime) -> datetime | None:
    """A posting time the chat model wrote, in IST: '18:30', '6pm', '8:15 am', or with a
    date, '2026-09-29 08:00' / '2026-09-29 8am'. Without a date it's the next such time;
    with one it must be in the future and within MAX_AHEAD. Anything else -> None."""
    _require_aware(now)
    dated = _DATED.match(text or "")
    clock = dated.group(4) if dated else (text or "")
    hm = parse_hhmm(clock)
    if hm is None and (m := _AMPM.match(clock)):
        hm = (int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0), int(m.group(2) or 0))
    if hm is None:
        return None
    if not dated:
        return next_ist_occurrence(hm[0], hm[1], now)
    try:
        day = datetime(int(dated.group(1)), int(dated.group(2)), int(dated.group(3)), hm[0], hm[1], tzinfo=IST)
    except ValueError:
        return None
    when = day.astimezone(UTC)
    return when if now < when <= now + MAX_AHEAD else None


def ist_day_start(now: datetime) -> datetime:
    """Midnight of the current IST date, as a UTC instant (SPEC §5.4 --fallback)."""
    _require_aware(now)
    local = now.astimezone(IST)
    return datetime.combine(local.date(), time(0, 0), tzinfo=IST).astimezone(UTC)


def format_ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%a %d %b, %I:%M %p IST")


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse a Postgres/ISO timestamp into an aware datetime (UTC if no offset)."""
    if value is None or isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
