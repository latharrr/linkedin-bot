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
