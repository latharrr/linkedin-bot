from datetime import UTC, datetime, timedelta

import pytest

from app.timeutil import (
    IST,
    custom_time,
    ist_day_start,
    next_6pm,
    next_9am,
    parse_hhmm,
    parse_ts,
)


def ist(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def test_next_9am_later_today():
    now = ist(2026, 9, 22, 7, 30)
    assert next_9am(now) == ist(2026, 9, 22, 9, 0).astimezone(UTC)


def test_next_9am_after_nine_rolls_to_tomorrow():
    assert next_9am(ist(2026, 9, 22, 9, 0, 1)) == ist(2026, 9, 23, 9, 0)


def test_exactly_9am_is_not_next():
    assert next_9am(ist(2026, 9, 22, 9, 0)) == ist(2026, 9, 23, 9, 0)


def test_next_6pm():
    assert next_6pm(ist(2026, 9, 22, 17, 59)) == ist(2026, 9, 22, 18, 0)
    assert next_6pm(ist(2026, 9, 22, 18, 1)) == ist(2026, 9, 23, 18, 0)


def test_results_are_utc():
    result = next_9am(ist(2026, 9, 22, 7, 0))
    assert result.utcoffset() == timedelta(0)
    assert result == datetime(2026, 9, 22, 3, 30, tzinfo=UTC)


def test_utc_input_uses_ist_date_not_utc_date():
    # 20:00 UTC on the 22nd is 01:30 IST on the 23rd -> next 9am IST is the 23rd.
    now = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
    assert next_9am(now) == ist(2026, 9, 23, 9, 0)


def test_month_and_year_rollover():
    assert next_6pm(ist(2026, 12, 31, 19, 0)) == ist(2027, 1, 1, 18, 0)


@pytest.mark.parametrize("text,expected", [("18:30", (18, 30)), ("9:05", (9, 5)), ("09.05", (9, 5)), (" 00:00 ", (0, 0)), ("23:59", (23, 59))])
def test_parse_hhmm_ok(text, expected):
    assert parse_hhmm(text) == expected


@pytest.mark.parametrize("text", ["24:00", "12:60", "noon", "1830", "6pm", "", "12:5", "-1:00"])
def test_parse_hhmm_rejects(text):
    assert parse_hhmm(text) is None


def test_custom_time_future_today():
    assert custom_time("18:30", ist(2026, 9, 22, 10, 0)) == ist(2026, 9, 22, 18, 30)


def test_custom_time_in_past_rolls_to_tomorrow():
    assert custom_time("08:15", ist(2026, 9, 22, 10, 0)) == ist(2026, 9, 23, 8, 15)


def test_custom_time_same_minute_counts_as_past():
    assert custom_time("10:00", ist(2026, 9, 22, 10, 0, 30)) == ist(2026, 9, 23, 10, 0)


def test_custom_time_invalid():
    assert custom_time("tomorrow", ist(2026, 9, 22, 10, 0)) is None


def test_ist_day_start_differs_from_utc_day():
    # 02:00 IST on the 23rd == 20:30 UTC on the 22nd. IST day started at 18:30 UTC on the 22nd.
    now = datetime(2026, 9, 22, 20, 30, tzinfo=UTC)
    assert ist_day_start(now) == datetime(2026, 9, 22, 18, 30, tzinfo=UTC)
    # A UTC-midnight boundary would wrongly be the 22nd 00:00 UTC.
    assert ist_day_start(now) != datetime(2026, 9, 22, 0, 0, tzinfo=UTC)


def test_ist_day_start_late_evening_ist():
    now = ist(2026, 9, 22, 23, 59)
    assert ist_day_start(now) == datetime(2026, 9, 21, 18, 30, tzinfo=UTC)


def test_naive_datetimes_rejected():
    with pytest.raises(ValueError):
        next_9am(datetime(2026, 9, 22, 7, 0))
    with pytest.raises(ValueError):
        ist_day_start(datetime(2026, 9, 22, 7, 0))


def test_parse_ts():
    assert parse_ts("2026-09-22T10:00:00Z") == datetime(2026, 9, 22, 10, tzinfo=UTC)
    assert parse_ts("2026-09-22T10:00:00") == datetime(2026, 9, 22, 10, tzinfo=UTC)
    assert parse_ts(None) is None
