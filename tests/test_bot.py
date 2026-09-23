import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

import bot as botmod
from app.telegram_ui import (
    FOOTER,
    TelegramMessenger,
    build_callback,
    draft_keyboard,
    stuck_keyboard,
    time_keyboard,
)
from app.timeutil import IST
from bot import BotHandler, fallback, nudge, parse_stats_lines, token_check
from tests.conftest import CHAT_ID

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)
ALL_BUTTONS = [
    ("pick", "a"), ("pick", "b"), ("time", "now"), ("time", "9am"), ("time", "6pm"), ("time", "custom"),
    ("regen", None), ("edit", "a"), ("edit", "b"), ("live", None), ("notlive", None),
]
VALID_STATUS = {"pick": "awaiting_choice", "time": "awaiting_choice", "regen": "awaiting_choice", "edit": "awaiting_choice", "live": "posting", "notlive": "posting"}
LONG_EDIT = "My own rewrite of the post.\nShort lines.\nWhat do you think?"


@pytest.fixture
def h(svc):
    return BotHandler(svc, now=lambda: NOW)


async def drain(h: BotHandler) -> None:
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


async def tap(h, action, post_id, arg=None, chat_id=CHAT_ID):
    await h.on_callback(chat_id, "cbid", build_callback(action, post_id, arg))


# ── whitelist (through real PTB Update objects) ─────────────────────────────
def _msg_update(chat_id: int, text: str) -> Update:
    msg = Message(message_id=1, date=NOW, chat=Chat(id=chat_id, type="private"), text=text)
    return Update(update_id=1, message=msg)


def _cb_update(chat_id: int, data: str) -> Update:
    msg = Message(message_id=1, date=NOW, chat=Chat(id=chat_id, type="private"), text="x")
    cq = CallbackQuery(id="cq1", from_user=User(id=chat_id, first_name="u", is_bot=False), chat_instance="ci", data=data, message=msg)
    return Update(update_id=2, callback_query=cq)


async def test_stranger_message_ignored_entirely(h, fakes):
    await h.on_update(_msg_update(999, "write me a post about crypto please"))
    await drain(h)
    assert fakes["messenger"].sent == [] and fakes["nvidia"].chat_calls == [] and fakes["db"].posts == {}


async def test_stranger_callback_ignored(h, fakes):
    post = fakes["db"].add_post()
    await h.on_update(_cb_update(999, build_callback("pick", post.id, "a")))
    assert fakes["messenger"].sent == [] and fakes["db"].posts[post.id].get("chosen") is None


async def test_owner_update_routed(h, fakes):
    post = fakes["db"].add_post()
    await h.on_update(_cb_update(CHAT_ID, build_callback("pick", post.id, "a")))
    assert fakes["db"].posts[post.id]["chosen"] == "a"
    assert fakes["messenger"].answers() == ["Draft A"]


async def test_non_text_message_ignored(h, fakes):
    msg = Message(message_id=1, date=NOW, chat=Chat(id=CHAT_ID, type="private"))
    await h.on_update(Update(update_id=3, message=msg))
    assert fakes["messenger"].sent == []


async def test_handler_error_is_reported_not_raised(h, fakes, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(fakes["db"], "get_chat_state", boom)
    await h.on_update(_msg_update(CHAT_ID, "a perfectly fine brief"))
    assert "Something went wrong" in fakes["messenger"].texts()[-1]


# ── text routing ─────────────────────────────────────────────────────────────
async def test_brief_runs_pipeline_in_background(h, fakes):
    await h.on_text(CHAT_ID, "make a post about student founders quitting after year one")
    assert fakes["messenger"].texts()[0].startswith("On it")
    await drain(h)
    assert len(fakes["db"].posts) == 1
    assert fakes["messenger"].texts()[-1] == FOOTER


async def test_generation_failure_reported(h, fakes):
    fakes["db"].voice = None
    await h.on_text(CHAT_ID, "draft: student founders quitting")
    await drain(h)
    assert fakes["messenger"].texts()[-1].startswith("Generation failed: PipelineError")


@pytest.mark.parametrize("text", ["ok", "thanks!", "hey", "student founders quitting after year one"])
async def test_plain_messages_are_chat_not_briefs(h, fakes, text):
    await h.on_text(CHAT_ID, text)
    await drain(h)
    assert fakes["db"].posts == {} and fakes["nvidia"].image_calls == []
    assert fakes["messenger"].texts() == ["Happy to help. What's on your mind?"]


async def test_overlong_message_gets_hint_without_model_call(h, fakes):
    await h.on_text(CHAT_ID, "x" * 801)
    assert fakes["nvidia"].chat_calls == [] and fakes["db"].posts == {}
    assert "edit window expired" in fakes["messenger"].texts()[0]


@pytest.mark.parametrize("cmd", ["/start", "/help", "/foo"])
async def test_commands_never_trigger_generation(h, fakes, cmd):
    await h.on_text(CHAT_ID, cmd)
    await drain(h)
    assert fakes["db"].posts == {} and fakes["nvidia"].chat_calls == []


async def test_state_checked_before_commands(h, fakes):
    post = fakes["db"].add_post()
    await fakes["db"].set_chat_state(CHAT_ID, "awaiting_edit_a", post.id, NOW)
    await h.on_text(CHAT_ID, "/stats " + LONG_EDIT)
    assert fakes["db"].posts[post.id]["draft_a"] == "/stats " + LONG_EDIT


async def test_expired_state_is_ignored(h, fakes):
    post = fakes["db"].add_post()
    await fakes["db"].set_chat_state(CHAT_ID, "awaiting_edit_a", post.id, NOW - timedelta(minutes=31))
    await h.on_text(CHAT_ID, "make a post about a brand new idea: agents")
    await drain(h)
    assert fakes["db"].posts[post.id]["draft_a"] == "draft A text"
    assert len(fakes["db"].posts) == 2  # treated as a new request


async def test_cancel_clears_state(h, fakes):
    await fakes["db"].set_chat_state(CHAT_ID, "awaiting_custom_time", None, NOW)
    await h.on_text(CHAT_ID, "/cancel")
    assert CHAT_ID not in fakes["db"].chat_states


# ── status guards: every button rejects every wrong status ───────────────────
@pytest.mark.parametrize("action,arg", ALL_BUTTONS)
@pytest.mark.parametrize("status", ["draft", "awaiting_choice", "queued", "posting", "posted", "failed"])
async def test_stale_callbacks_answer_expired_and_change_nothing(h, fakes, action, arg, status):
    if status == VALID_STATUS[action]:
        pytest.skip("valid combination")
    post = fakes["db"].add_post(status=status, chosen="a")
    before = dict(fakes["db"].posts[post.id])
    await tap(h, action, post.id, arg)
    await drain(h)
    assert fakes["messenger"].answers() == ["Expired"]
    assert fakes["messenger"].texts() == []
    assert fakes["db"].posts[post.id] == before
    assert fakes["db"].chat_states == {}


@pytest.mark.parametrize("arg", ["now", "9am", "6pm", "custom"])
async def test_time_without_pick_is_expired(h, fakes, arg):
    post = fakes["db"].add_post()
    await tap(h, "time", post.id, arg)
    assert fakes["messenger"].answers() == ["Expired"]
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice"


@pytest.mark.parametrize("data", [None, "", "garbage", "pick:a:not-a-uuid", f"pick:z:{'0' * 8}-0000-0000-0000-{'0' * 12}"])
async def test_malformed_callback_answers_expired(h, fakes, data):
    await h.on_callback(CHAT_ID, "cbid", data)
    assert fakes["messenger"].answers() == ["Expired"]


async def test_unknown_post_and_foreign_chat_expired(h, fakes):
    await tap(h, "pick", "00000000-0000-0000-0000-000000000000", "a")
    post = fakes["db"].add_post(chat_id=777)
    await tap(h, "pick", post.id, "a")
    assert fakes["messenger"].answers() == ["Expired", "Expired"]


# ── happy paths ──────────────────────────────────────────────────────────────
async def test_pick_then_now(h, fakes):
    post = fakes["db"].add_post()
    await tap(h, "pick", post.id, "b")
    assert fakes["db"].posts[post.id]["chosen"] == "b"
    assert fakes["messenger"].keyboards()[-1] == time_keyboard(post.id)
    await tap(h, "time", post.id, "now")
    row = fakes["db"].posts[post.id]
    assert row["status"] == "queued" and row["scheduled_at"] == NOW + timedelta(minutes=2)
    assert fakes["messenger"].texts()[-1].startswith("Queued ✓ Draft B")


@pytest.mark.parametrize("arg,expected", [("9am", datetime(2026, 9, 23, 9, 0, tzinfo=IST)), ("6pm", datetime(2026, 9, 22, 18, 0, tzinfo=IST))])
async def test_time_9am_6pm(h, fakes, arg, expected):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, arg)
    assert fakes["db"].posts[post.id]["scheduled_at"] == expected


async def test_double_tap_time_second_is_expired(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, "now")
    await tap(h, "time", post.id, "6pm")
    assert fakes["messenger"].answers() == ["Queued", "Expired"]
    assert fakes["db"].posts[post.id]["scheduled_at"] == NOW + timedelta(minutes=2)


async def test_custom_time_flow_rolls_past_time_to_tomorrow(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, "custom")
    assert fakes["db"].chat_states[CHAT_ID]["pending_action"] == "awaiting_custom_time"
    assert fakes["messenger"].texts()[-1] == "Send time as HH:MM (IST)"
    await h.on_text(CHAT_ID, "noonish")
    assert CHAT_ID in fakes["db"].chat_states  # bad input keeps state
    await h.on_text(CHAT_ID, "08:30")  # it's 10:00 IST → tomorrow
    row = fakes["db"].posts[post.id]
    assert row["status"] == "queued" and row["scheduled_at"] == datetime(2026, 9, 23, 8, 30, tzinfo=IST)
    assert CHAT_ID not in fakes["db"].chat_states


async def test_custom_time_after_post_locked_is_expired(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, "custom")
    fakes["db"].posts[post.id]["status"] = "queued"
    await h.on_text(CHAT_ID, "18:00")
    assert fakes["messenger"].texts()[-1].startswith("Expired")


async def test_edit_flow_sets_only_that_flag_and_resends_preview(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "edit", post.id, "b")
    assert fakes["messenger"].texts()[-1] == "Send your edited version of draft B"
    await h.on_text(CHAT_ID, LONG_EDIT)
    row = fakes["db"].posts[post.id]
    assert row["draft_b"] == LONG_EDIT and row["edited_b"] is True and row["edited_a"] is False
    assert row["chosen"] is None
    assert CHAT_ID not in fakes["db"].chat_states
    assert fakes["messenger"].keyboards()[-1] == draft_keyboard(post.id, two=True)  # older two-draft row


async def test_save_edit_on_locked_post_a3(h, fakes):
    post = fakes["db"].add_post()
    await tap(h, "edit", post.id, "a")
    fakes["db"].posts[post.id]["status"] = "queued"
    await h.on_text(CHAT_ID, LONG_EDIT)
    assert fakes["messenger"].texts()[-1] == "Draft already locked"
    assert fakes["db"].posts[post.id]["draft_a"] == "draft A text"
    assert CHAT_ID not in fakes["db"].chat_states


async def test_edit_text_during_regen_is_rejected(h, fakes):
    post = fakes["db"].add_post()
    await tap(h, "edit", post.id, "a")  # edit window opened...
    h.regenerating.add(post.id)  # ...then a regen started
    await h.on_text(CHAT_ID, LONG_EDIT)
    assert fakes["db"].posts[post.id]["draft_a"] == "draft A text"
    assert fakes["messenger"].texts()[-1].startswith("Regenerating")
    assert CHAT_ID not in fakes["db"].chat_states


async def test_edit_rejects_too_short_and_too_long(h, fakes):
    post = fakes["db"].add_post()
    await tap(h, "edit", post.id, "a")
    await h.on_text(CHAT_ID, "ok")
    await h.on_text(CHAT_ID, "_" * 1600)  # 3200 chars once escaped
    assert fakes["db"].posts[post.id]["draft_a"] == "draft A text"
    assert CHAT_ID in fakes["db"].chat_states


async def test_regen_resets_flags_and_blocks_concurrent_taps(h, fakes):
    post = fakes["db"].add_post(edited_a=True, edited_b=True)
    await tap(h, "regen", post.id)
    await tap(h, "regen", post.id)
    await tap(h, "pick", post.id, "a")
    assert fakes["messenger"].answers() == ["Regenerating", "Working on it — wait for the update", "Working on it — wait for the update"]
    await drain(h)
    row = fakes["db"].posts[post.id]
    assert row["edited_a"] is False and row["edited_b"] is False and row.get("chosen") is None
    assert post.id not in h.regenerating
    await tap(h, "pick", post.id, "a")
    assert fakes["messenger"].answers()[-1] == "Draft A"


# ── A2: stuck row Live ✓ / Not live ──────────────────────────────────────────
async def test_live_flow_marks_posted_and_admits_edited(h, fakes):
    db = fakes["db"]
    post = db.add_post(status="posting", chosen="a", edited_a=True, claimed_at=NOW - timedelta(hours=1), alerted_at=NOW)
    await tap(h, "live", post.id)
    assert db.chat_states[CHAT_ID]["pending_action"] == "awaiting_post_url"
    await h.on_text(CHAT_ID, "it's live")
    assert db.posts[post.id]["status"] == "posting" and CHAT_ID in db.chat_states
    url = "https://www.linkedin.com/feed/update/urn:li:share:123/"
    await h.on_text(CHAT_ID, url)
    row = db.posts[post.id]
    assert row["status"] == "posted" and row["post_url"] == url and row["posted_at"] == NOW
    assert row["past_post_id"] in db.past and db.past[row["past_post_id"]]["source"] == "ai"
    assert CHAT_ID not in db.chat_states


async def test_live_flow_unedited_not_admitted(h, fakes):
    post = fakes["db"].add_post(status="posting", chosen="a")
    await tap(h, "live", post.id)
    await h.on_text(CHAT_ID, "https://www.linkedin.com/feed/update/urn:li:share:1")
    assert fakes["db"].posts[post.id]["status"] == "posted" and fakes["db"].past == {}


async def test_notlive_requeues_and_resets(h, fakes):
    post = fakes["db"].add_post(status="posting", chosen="a", retry_count=1, claimed_at=NOW, alerted_at=NOW)
    await tap(h, "notlive", post.id)
    row = fakes["db"].posts[post.id]
    assert (row["status"], row["retry_count"], row["claimed_at"], row["alerted_at"]) == ("queued", 0, None, None)


def test_stuck_keyboard_buttons():
    pid = "11111111-1111-1111-1111-111111111111"
    assert stuck_keyboard(pid) == [[("Live ✓", f"live:{pid}"), ("Not live", f"notlive:{pid}")]]


# ── /stats (§5.3) ────────────────────────────────────────────────────────────
def _posted(db, days_ago: int, **kw):
    return db.add_post(status="posted", chosen="a", posted_at=NOW - timedelta(days=days_ago), post_url=f"https://www.linkedin.com/feed/update/{days_ago}", **kw)


async def _seed_humans(db, engagements):
    for e in engagements:
        await db.insert_past_post(f"human {e}", "human", [0.0], engagement=e)


async def test_stats_flow_updates_and_gate(h, fakes):
    db = fakes["db"]
    admitted_id = await db.insert_past_post("ai edited", "ai", [0.0])
    p1 = _posted(db, 1, past_post_id=admitted_id)
    p2 = _posted(db, 2)
    await _seed_humans(db, [0, 0, 10, 20])  # only 2 engaged humans → gate closed
    await h.on_text(CHAT_ID, "/stats")
    assert db.chat_states[CHAT_ID]["post_id"] == p1.id
    await h.on_text(CHAT_ID, "100 20\n500 50")
    assert db.past[admitted_id]["engagement"] == 120
    assert db.posts[p2.id].get("past_post_id") is None
    assert "gate closed" in fakes["messenger"].texts()[-1]


async def test_stats_promotion_when_gate_open(h, fakes):
    db = fakes["db"]
    winner, loser = _posted(db, 1), _posted(db, 2)
    await _seed_humans(db, [10, 20, 30, 40, 50])  # median 30
    await h.on_text(CHAT_ID, "/stats")
    await h.on_text(CHAT_ID, "25 6\n20 10")  # 31 beats 30; 30 does not
    assert db.posts[winner.id]["past_post_id"] in db.past
    assert db.past[db.posts[winner.id]["past_post_id"]]["engagement"] == 31
    assert db.posts[loser.id].get("past_post_id") is None


async def test_stats_wrong_line_count_keeps_state(h, fakes):
    _posted(fakes["db"], 1)
    await h.on_text(CHAT_ID, "/stats")
    await h.on_text(CHAT_ID, "1 2\n3 4")
    assert CHAT_ID in fakes["db"].chat_states
    assert "Expected 1 lines" in fakes["messenger"].texts()[-1]


async def test_stats_list_is_stable_if_a_new_post_lands(h, fakes):
    db = fakes["db"]
    old = _posted(db, 1)
    await h.on_text(CHAT_ID, "/stats")
    _posted(db, 0)  # newer post published before the reply
    await _seed_humans(db, [1, 1, 1, 1, 1])
    await h.on_text(CHAT_ID, "10 0")
    assert db.posts[old.id]["past_post_id"] is not None


async def test_stats_no_posts(h, fakes):
    await h.on_text(CHAT_ID, "/stats")
    assert fakes["messenger"].texts() == ["No posted posts yet."]


def test_parse_stats_lines():
    assert parse_stats_lines("120 14\n-\n85, 9", 3) == [(120, 14), None, (85, 9)]
    assert isinstance(parse_stats_lines("1 2", 2), str)
    assert isinstance(parse_stats_lines("abc\n1 2", 2), str)


# ── cron modes ───────────────────────────────────────────────────────────────
async def test_nudge(svc, fakes):
    await nudge(svc)
    assert fakes["messenger"].texts()[0].startswith("What's today's topic?")
    assert fakes["db"].chat_states[CHAT_ID]["pending_action"] == "awaiting_brief"  # the reply goes straight to drafts


async def test_fallback_skips_when_brief_today_ist(svc, fakes):
    # 20:00 UTC yesterday = 01:30 IST today → counts as today in IST.
    fakes["db"].add_post(created_at=datetime(2026, 9, 21, 20, 0, tzinfo=UTC))
    assert await fallback(svc, NOW) == "skipped"
    assert fakes["tavily"].queries == []


async def test_fallback_runs_when_only_yesterday_ist(svc, fakes):
    # 18:00 UTC yesterday = 23:30 IST yesterday → not today.
    fakes["db"].add_post(created_at=datetime(2026, 9, 21, 18, 0, tzinfo=UTC), status="posted")
    assert await fallback(svc, NOW) == "drafted"
    new = [r for r in fakes["db"].posts.values() if r["status"] == "awaiting_choice"]
    assert len(new) == 1  # drafts only
    assert fakes["messenger"].texts()[-1] == FOOTER


async def test_fallback_never_queues(svc, fakes):
    await fallback(svc, NOW)
    assert all(r["status"] == "awaiting_choice" for r in fakes["db"].posts.values())


async def test_token_check(svc, fakes):
    db = fakes["db"]
    assert await token_check(svc, NOW) == "missing"
    db.settings["linkedin_access_token"] = "tok"
    db.settings["linkedin_token_issued_at"] = (NOW - timedelta(days=51)).isoformat()
    assert await token_check(svc, NOW) == "warned"
    assert "51 days old" in fakes["messenger"].texts()[-1]
    db.settings["linkedin_token_issued_at"] = (NOW - timedelta(days=10)).isoformat()
    assert await token_check(svc, NOW) == "ok"


# ── plain text only (A4) and no LinkedIn from the bot ────────────────────────
async def test_messenger_never_sets_parse_mode():
    calls = []

    class FakeBot:
        async def send_message(self, **kw):
            calls.append(kw)

        async def send_photo(self, **kw):
            calls.append(kw)

        async def answer_callback_query(self, **kw):
            calls.append(kw)

    m = TelegramMessenger(FakeBot())  # type: ignore[arg-type]
    await m.send_text(1, "*not bold* <b>x</b>", [[("A", "pick:a:x")]])
    await m.send_photo(1, "https://x/y.png")
    await m.answer_callback("c", "ok")
    assert calls and all("parse_mode" not in kw for kw in calls)
    assert calls[0]["text"] == "*not bold* <b>x</b>"


def test_bot_has_no_linkedin_posting_path():
    src = Path(botmod.__file__).read_text()
    assert "LinkedInClient" not in src and "create_post" not in src and "api.linkedin.com" not in src
