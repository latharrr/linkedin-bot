import uuid

import pytest

from app.telegram_ui import (
    MAX_CALLBACK_BYTES,
    Callback,
    build_callback,
    draft_keyboard,
    guard_allows,
    parse_callback,
    stuck_keyboard,
    time_keyboard,
)

PID = str(uuid.uuid4())

ALL_BUTTONS = [
    ("pick", "a"), ("pick", "b"),
    ("time", "now"), ("time", "9am"), ("time", "6pm"), ("time", "custom"),
    ("regen", None),
    ("edit", "a"), ("edit", "b"),
    ("live", None), ("notlive", None),
]
STATUSES = ["draft", "awaiting_choice", "queued", "posting", "posted", "failed"]


@pytest.mark.parametrize("action,arg", ALL_BUTTONS)
def test_roundtrip_and_under_64_bytes(action, arg):
    data = build_callback(action, PID, arg)
    assert len(data.encode()) <= MAX_CALLBACK_BYTES
    assert parse_callback(data) == Callback(action, arg, PID)


def test_longest_callback_fits_with_margin():
    longest = max(len(build_callback(a, PID, g).encode()) for a, g in ALL_BUTTONS)
    assert longest == len("time:custom:") + 36 < 64


def test_spec_formats():
    assert build_callback("pick", PID, "a") == f"pick:a:{PID}"
    assert build_callback("regen", PID) == f"regen:{PID}"
    assert build_callback("live", PID) == f"live:{PID}"
    assert build_callback("notlive", PID) == f"notlive:{PID}"


@pytest.mark.parametrize(
    "bad",
    [
        None, "", "pick", "pick:a", f"pick:c:{PID}", f"pick:{PID}", f"regen:a:{PID}",
        f"time:noon:{PID}", "pick:a:not-a-uuid", f"nuke:{PID}", f"edit:a:{PID}:extra",
        "x" * 65, f"live:a:{PID}",
    ],
)
def test_parse_rejects_malformed(bad):
    assert parse_callback(bad) is None


@pytest.mark.parametrize("action,arg", [("pick", None), ("pick", "c"), ("regen", "a"), ("bogus", None)])
def test_build_rejects_bad_input(action, arg):
    with pytest.raises(ValueError):
        build_callback(action, PID, arg)


def test_build_rejects_non_uuid():
    with pytest.raises(ValueError):
        build_callback("regen", "123")


def test_keyboards_carry_post_id_everywhere():
    for kb in (draft_keyboard(PID), time_keyboard(PID), stuck_keyboard(PID)):
        for row in kb:
            for _label, data in row:
                assert parse_callback(data).post_id == PID
    assert [lbl for row in stuck_keyboard(PID) for lbl, _ in row] == ["Live ✓", "Not live"]


# ── Status guards: every button, every status ──────────────────────────────
def _allowed(action: str, status: str, chosen: str | None) -> bool:
    if action in ("pick", "regen", "edit"):
        return status == "awaiting_choice"
    if action == "time":
        return status == "awaiting_choice" and chosen is not None
    return status == "posting"  # live / notlive


@pytest.mark.parametrize("action,arg", ALL_BUTTONS)
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("chosen", [None, "a"])
def test_guard_matrix(action, arg, status, chosen):
    cb = Callback(action, arg, PID)
    assert guard_allows(cb, status, chosen) is _allowed(action, status, chosen)


@pytest.mark.parametrize("action,arg", ALL_BUTTONS)
def test_missing_post_rejected(action, arg):
    assert guard_allows(Callback(action, arg, PID), None, None) is False


@pytest.mark.parametrize("arg", ["now", "9am", "6pm", "custom"])
def test_time_buttons_need_a_pick_first(arg):
    assert not guard_allows(Callback("time", arg, PID), "awaiting_choice", None)
    assert guard_allows(Callback("time", arg, PID), "awaiting_choice", "b")
