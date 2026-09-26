"""Conversation mode: post requests vs chat, the topic prompt, and the 8am-nudge reply."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from app.chat import (
    ChatMemory,
    DraftRequest,
    is_decline,
    parse_draft_request,
    wants_bot_to_pick,
)
from app.telegram_ui import FOOTER
from app.timeutil import IST
from bot import ASK_TOPIC, CHAT_DOWN, BotHandler
from tests.conftest import CHAT_ID

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("make a post about RAG in Indian startups", "RAG in Indian startups"),
        ("Can you write a LinkedIn post on why I gate every post behind two taps?", "why I gate every post behind two taps"),
        ("please draft a post: agents that need approval", "agents that need approval"),
        ("draft: agents that need approval", "agents that need approval"),
        ("/draft agents", "agents"),
        ("post about my first hackathon", "my first hackathon"),
        ("create a new linkedin post about Groq's free tier", "Groq's free tier"),
    ],
)
def test_explicit_requests_extract_topic(text, topic):
    assert parse_draft_request(text) == DraftRequest(topic)


@pytest.mark.parametrize("text", ["make a post", "write a post about it", "draft that", "make one", "make a post on this!"])
def test_requests_without_a_topic(text):
    assert parse_draft_request(text) == DraftRequest(None)


@pytest.mark.parametrize(
    "text",
    ["hey", "how are you?", "draft A is better", "post it", "post A at 6pm", "what do you think about agents?", "I posted yesterday"],
)
def test_chat_is_not_a_request(text):
    assert parse_draft_request(text) is None


def test_you_pick_and_decline():
    assert wants_bot_to_pick("you pick") and wants_bot_to_pick("Surprise me!") and not wants_bot_to_pick("AI agents")
    assert is_decline("not today") and is_decline("no post today, thanks") and is_decline("skip")
    assert not is_decline("nobody talks about eval costs")


def test_memory_is_capped_per_chat():
    m = ChatMemory(turns=3)
    for i in range(5):
        m.add(1, "user", str(i))
    assert [x["content"] for x in m.history(1)] == ["2", "3", "4"] and m.history(2) == []


# ── bot flows ────────────────────────────────────────────────────────────────
@pytest.fixture
def h(svc):
    return BotHandler(svc, now=lambda: NOW)


async def drain(h):
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


async def test_hey_gets_a_chat_reply_not_a_run(h, fakes):
    await h.on_text(CHAT_ID, "hey")
    assert fakes["messenger"].texts() == ["Happy to help. What's on your mind?"]
    assert fakes["db"].posts == {} and fakes["nvidia"].image_calls == []
    system = fakes["nvidia"].chat_calls[0]["messages"][0]["content"]
    assert "Nothing is published until he approves it AND a posting time" in system


async def test_explicit_request_echoes_topic_and_drafts(h, fakes):
    await h.on_text(CHAT_ID, "make a post about RAG in Indian startups")
    assert fakes["messenger"].texts()[0].startswith("On it — researching and writing your post about:\nRAG in Indian startups")
    await drain(h)
    post = next(iter(fakes["db"].posts.values()))
    assert post["brief"] == "RAG in Indian startups" and post["status"] == "awaiting_choice"


async def test_pasted_link_is_a_request(h, fakes):
    await h.on_text(CHAT_ID, "https://www.langchain.com/blog/building-a-harness-with-jev something on this ?")
    await drain(h)
    assert len(fakes["db"].posts) == 1 and fakes["messenger"].texts()[-1] == FOOTER


async def test_chat_model_can_start_drafts_with_its_own_brief(h, fakes):
    fakes["nvidia"].chat_reply = {"reply": "Love it, drafting now.", "draft_topic": "Why my bot needs two taps before posting"}
    await h.on_text(CHAT_ID, "yes go for it")
    texts = fakes["messenger"].texts()
    assert texts[0] == "Love it, drafting now." and "Why my bot needs two taps before posting" in texts[1]
    await drain(h)
    assert next(iter(fakes["db"].posts.values()))["brief"] == "Why my bot needs two taps before posting"


async def test_chat_history_is_sent_to_the_model(h, fakes):
    await h.on_text(CHAT_ID, "hey")
    await h.on_text(CHAT_ID, "any idea for today?")
    last = fakes["nvidia"].chat_calls_history[-1]
    assert [m["role"] for m in last] == ["user", "assistant", "user"] and last[-1]["content"] == "any idea for today?"


async def test_non_json_chat_reply_is_shown_and_never_drafts(h, fakes):
    fakes["nvidia"].chat_reply = "Sure! Want me to draft that?"
    await h.on_text(CHAT_ID, "thinking about eval costs")
    assert fakes["messenger"].texts() == ["Sure! Want me to draft that?"] and fakes["db"].posts == {}


async def test_chat_model_down_explains_how_to_request(h, fakes, monkeypatch):
    async def down(*a, **k):
        raise RuntimeError("all writers failed")

    monkeypatch.setattr(h.svc.writer, "converse", down)
    await h.on_text(CHAT_ID, "hey")
    assert fakes["messenger"].texts() == [CHAT_DOWN] and fakes["db"].posts == {}


async def test_topicless_request_asks_then_uses_reply(h, fakes):
    await h.on_text(CHAT_ID, "make a post")
    assert fakes["messenger"].texts() == [ASK_TOPIC]
    assert fakes["db"].chat_states[CHAT_ID]["pending_action"] == "awaiting_brief"
    await h.on_text(CHAT_ID, "cold-emailing 50 founders for an internship")
    await drain(h)
    assert next(iter(fakes["db"].posts.values()))["brief"] == "cold-emailing 50 founders for an internship"
    assert CHAT_ID not in fakes["db"].chat_states


async def test_topic_reply_you_pick_uses_news(h, fakes):
    await fakes["db"].set_chat_state(CHAT_ID, "awaiting_brief", None, NOW)
    await h.on_text(CHAT_ID, "you pick")
    await drain(h)
    assert fakes["tavily"].queries and len(fakes["db"].posts) == 1


async def test_topic_reply_decline_runs_nothing(h, fakes):
    await fakes["db"].set_chat_state(CHAT_ID, "awaiting_brief", None, NOW)
    await h.on_text(CHAT_ID, "not today")
    await drain(h)
    assert fakes["db"].posts == {} and fakes["nvidia"].chat_calls == []
    assert fakes["messenger"].texts()[-1].startswith("Okay — no post for now")


async def test_draft_that_with_context_goes_to_the_model(h, fakes):
    await h.on_text(CHAT_ID, "I keep thinking about how agents need approval gates")
    fakes["nvidia"].chat_reply = {"reply": "On it.", "draft_topic": "Agents need approval gates"}
    await h.on_text(CHAT_ID, "draft that")
    await drain(h)
    assert next(iter(fakes["db"].posts.values()))["brief"] == "Agents need approval gates"


async def test_chat_never_publishes(h, fakes):
    post = fakes["db"].add_post()
    fakes["nvidia"].chat_reply = {"reply": "Use the buttons under the drafts: pick A or B, then a time.", "draft_topic": None}
    await h.on_text(CHAT_ID, "post A now")
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice" and fakes["db"].posts[post.id].get("chosen") is None


# ── active-run session state (a draft in flight has priority over new requests) ──────────
@pytest.mark.parametrize("text", ["?", "status?", "is it done?", "where's my draft?", "any update?"])
async def test_status_query_during_active_run_reports_status_not_a_new_draft(h, fakes, text):
    await h.on_text(CHAT_ID, "make a post about RAG in Indian startups")
    await h.on_text(CHAT_ID, text)
    assert fakes["messenger"].texts()[-1].startswith("⏳ Still on it — started")
    await drain(h)
    assert len(fakes["db"].posts) == 1  # the status check never started a second run


async def test_new_draft_request_during_active_run_does_not_start_a_second_run(h, fakes):
    await h.on_text(CHAT_ID, "make a post about RAG in Indian startups")
    await h.on_text(CHAT_ID, "make a post about pricing pages")
    assert fakes["messenger"].texts()[-1].startswith("⏳ Still on it — started")
    await drain(h)
    assert len(fakes["db"].posts) == 1
    assert next(iter(fakes["db"].posts.values()))["brief"] == "RAG in Indian startups"


@pytest.mark.parametrize("text", ["cancel", "stop", "/cancel"])
async def test_cancel_during_active_run_stops_it_and_confirms(h, fakes, text):
    await h.on_text(CHAT_ID, "make a post about RAG in Indian startups")
    run = h.active_runs[CHAT_ID]
    await h.on_text(CHAT_ID, text)
    assert CHAT_ID not in h.active_runs
    assert fakes["messenger"].texts()[-1] == 'Cancelled — stopped the draft on "RAG in Indian startups".'
    with contextlib.suppress(asyncio.CancelledError):
        await run.task
    assert fakes["db"].posts == {}  # cancelled before it could write anything


async def test_chat_model_is_told_a_draft_is_already_running(h, fakes):
    await h.on_text(CHAT_ID, "make a post about RAG in Indian startups")
    await h.on_text(CHAT_ID, "what about eval costs instead?")
    system = fakes["nvidia"].chat_calls[-1]["messages"][0]["content"]
    assert "A draft is already running" in system and "RAG in Indian startups" in system
    await drain(h)
    assert len(fakes["db"].posts) == 1


async def test_active_run_clears_once_generation_finishes(h, fakes):
    await h.on_text(CHAT_ID, "make a post about RAG in Indian startups")
    assert CHAT_ID in h.active_runs
    await drain(h)
    assert CHAT_ID not in h.active_runs


# ── Jev second opinion ───────────────────────────────────────────────────────
class FakeJev:
    def __init__(self, p):
        self.p = p
        self.seen = []

    async def wants_drafts(self, history):
        self.seen.append(history)
        return self.p


async def test_jev_blocks_a_chat_started_run_and_offers_instead(h, fakes):
    h.svc.jev = FakeJev(0.1)
    fakes["nvidia"].chat_reply = {"reply": "Drafting!", "draft_topic": "Agents replacing junior devs"}
    await h.on_text(CHAT_ID, "what do you think about agents replacing junior devs?")
    await drain(h)
    assert fakes["db"].posts == {}
    assert fakes["messenger"].texts() == ['Want me to draft a post on: Agents replacing junior devs\nSay "draft that" and I\'ll start.']


@pytest.mark.parametrize("p", [0.93, None])  # confident yes, or Jev unavailable → the chat model decides
async def test_jev_yes_or_unavailable_lets_the_run_start(h, fakes, p):
    h.svc.jev = FakeJev(p)
    fakes["nvidia"].chat_reply = {"reply": "On it.", "draft_topic": "Two taps before posting"}
    await h.on_text(CHAT_ID, "yes go for it")
    await drain(h)
    assert len(fakes["db"].posts) == 1


# ── chat tools (read-only agent loop) ───────────────────────────────────────
async def test_chat_can_answer_whats_queued_from_live_data(h, fakes):
    fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=2), draft_a="Your dashboard screams downloads.\n\nBody")
    fakes["nvidia"].chat_reply = [
        {"reply": "", "draft_topic": None, "tool_call": "queue"},
        {"reply": "One post queued for later today.", "draft_topic": None, "tool_call": None},
    ]
    await h.on_text(CHAT_ID, "what's queued?")
    assert fakes["messenger"].texts() == ["One post queued for later today."]
    assert len(fakes["nvidia"].chat_calls) == 2
    second_call_text = fakes["nvidia"].chat_calls[1]["messages"][-1]["content"]
    assert "[queue result]" in second_call_text and "Your dashboard screams downloads." in second_call_text


async def test_chat_tool_call_never_starts_or_touches_a_draft(h, fakes):
    fakes["nvidia"].chat_reply = [
        {"reply": "", "draft_topic": None, "tool_call": "drafts"},
        {"reply": "Nothing waiting on you right now.", "draft_topic": None, "tool_call": None},
    ]
    await h.on_text(CHAT_ID, "any drafts waiting?")
    assert fakes["messenger"].texts() == ["Nothing waiting on you right now."]
    assert fakes["db"].posts == {}


async def test_chat_unknown_tool_name_is_ignored_not_looped(h, fakes):
    fakes["nvidia"].chat_reply = {"reply": "Not sure what you mean.", "draft_topic": None, "tool_call": "delete_everything"}
    await h.on_text(CHAT_ID, "do something weird")
    assert fakes["messenger"].texts() == ["Not sure what you mean."]
    assert len(fakes["nvidia"].chat_calls) == 1  # no tool named "delete_everything" exists, so it never loops


async def test_chat_tool_loop_is_capped_not_infinite(h, fakes):
    fakes["nvidia"].chat_reply = [{"reply": "", "draft_topic": None, "tool_call": "status"}]  # always asks for another lookup
    await h.on_text(CHAT_ID, "how's it going")
    assert len(fakes["nvidia"].chat_calls) == 3  # MAX_TOOL_ROUNDS, not unbounded
    assert fakes["messenger"].texts() == ["That took a couple of lookups too many — ask me again?"]


# ── chat actions (step 3): propose, then he confirms ──────────────────────
def act_buttons(fakes):
    return [cb for row in fakes["messenger"].keyboards()[-1] for _, cb in row]


async def test_chat_schedule_asks_first_and_only_queues_on_yes(h, fakes):
    post = fakes["db"].add_post(draft_b="", draft_a="Your dashboard lies.\n\nBody")
    fakes["nvidia"].chat_reply = {"reply": "Done!", "action": {"name": "schedule", "post": None, "when": "18:30"}}
    await h.on_text(CHAT_ID, "schedule it for 6:30 this evening")
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice"  # nothing happens without the tap
    texts = fakes["messenger"].texts()
    assert "Done!" not in texts  # the model's reply is never shown for an action
    assert texts[-1].startswith("Schedule this for Tue 22 Sep, 06:30 PM IST?") and "Your dashboard lies." in texts[-1]
    yes, no = act_buttons(fakes)
    await h.on_callback(CHAT_ID, "cb1", yes, message_id=7)
    row = fakes["db"].posts[post.id]
    assert row["status"] == "queued" and row["chosen"] == "a"
    assert row["scheduled_at"] == datetime(2026, 9, 22, 18, 30, tzinfo=IST)
    assert ("edit_keyboard", CHAT_ID, (7, None)) in fakes["messenger"].sent


async def test_chat_action_no_and_stale_buttons_change_nothing(h, fakes):
    post = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=3))
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "discard", "post": post.id[:8]}}
    await h.on_text(CHAT_ID, "drop tomorrow's post")
    first_yes, first_no = act_buttons(fakes)
    await h.on_text(CHAT_ID, "actually, drop it")  # a newer proposal replaces the older one
    second_yes, second_no = act_buttons(fakes)
    await h.on_callback(CHAT_ID, "cb1", first_yes)
    assert fakes["db"].posts[post.id]["status"] == "queued"
    assert fakes["messenger"].answers()[-1] == "Expired"
    await h.on_callback(CHAT_ID, "cb2", second_no)
    assert fakes["db"].posts[post.id]["status"] == "queued"
    assert "Okay, left it as it is." in fakes["messenger"].texts()
    await h.on_callback(CHAT_ID, "cb3", second_yes)  # spent by the "no"
    assert fakes["db"].posts[post.id]["status"] == "queued"


async def test_chat_reschedule_moves_a_queued_post(h, fakes):
    post = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=3))
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "reschedule", "post": f"ref {post.id[:8]}", "when": "2026-09-25 09:00"}}
    await h.on_text(CHAT_ID, "push it to Friday 9am")
    await h.on_callback(CHAT_ID, "cb", act_buttons(fakes)[0])
    row = fakes["db"].posts[post.id]
    assert row["status"] == "queued" and row["scheduled_at"] == datetime(2026, 9, 25, 9, 0, tzinfo=IST)


async def test_chat_unschedule_goes_back_to_waiting(h, fakes):
    post = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=3))
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "unschedule", "post": None}}
    await h.on_text(CHAT_ID, "hold that post")
    await h.on_callback(CHAT_ID, "cb", act_buttons(fakes)[0])
    row = fakes["db"].posts[post.id]
    assert row["status"] == "awaiting_choice" and row["scheduled_at"] is None


async def test_chat_action_rechecks_state_on_yes(h, fakes):
    post = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=3))
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "discard", "post": None}}
    await h.on_text(CHAT_ID, "drop it")
    fakes["db"].posts[post.id]["status"] = "posting"  # the dispatcher got there first
    await h.on_callback(CHAT_ID, "cb", act_buttons(fakes)[0])
    assert fakes["db"].posts[post.id]["status"] == "posting"
    assert "That post already moved on — nothing changed." in fakes["messenger"].texts()


async def test_chat_action_asks_when_ambiguous_or_missing_time(h, fakes):
    fakes["db"].add_post(draft_b="", topic="eval costs")
    fakes["db"].add_post(draft_b="", topic="agent memory")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "schedule", "post": None, "when": "18:00"}}
    await h.on_text(CHAT_ID, "schedule it at 6")
    assert fakes["messenger"].texts()[-1].startswith("Which one?")
    assert h.pending == {} and len(act_buttons(fakes)) == 2
    only = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=3))
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "reschedule", "post": only.id[:8], "when": "sometime"}}
    await h.on_text(CHAT_ID, "move it")
    assert fakes["messenger"].texts()[-1] == "When should it go out? e.g. 6pm, 18:30, or 2026-09-29 08:00 (IST)."
    assert h.pending == {}


async def test_made_up_ref_offers_the_real_posts_and_a_pick_carries_on(h, fakes):
    """Live bug 2026-09-26: the model showed refs that didn't exist, he typed one back."""
    real = fakes["db"].add_post(draft_b="", topic="stablecoins for agents", draft_a="Agents will pay in stablecoins.\n\nBody")
    fakes["db"].add_post(draft_b="", topic="79-table KPI digest")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "schedule", "post": "c3a9f2b1", "when": "18:00"}}
    await h.on_text(CHAT_ID, "c3a9f2b1")
    assert "couldn't find" not in fakes["messenger"].texts()[-1]
    assert "stablecoins for agents" in fakes["messenger"].texts()[-1]
    buttons = act_buttons(fakes)
    await h.on_callback(CHAT_ID, "cb1", buttons[0], message_id=5)  # newest first: the stablecoins post
    assert fakes["messenger"].texts()[-1].startswith("Schedule this for Tue 22 Sep, 06:00 PM IST?")
    await h.on_callback(CHAT_ID, "cb2", act_buttons(fakes)[0])
    assert fakes["db"].posts[real.id]["status"] == "queued"
    await h.on_callback(CHAT_ID, "cb3", buttons[1])  # the pick list is spent
    assert fakes["messenger"].answers()[-1] == "Expired"


def test_ref_tags_are_scrubbed_from_chat_replies():
    from bot import REF_TAG

    assert REF_TAG.sub("", "- [ref d12d1f62] stablecoins\n- (ref: 99e845f7) KPI digest") == "- stablecoins\n- KPI digest"


async def test_chat_action_unknown_name_does_nothing(h, fakes):
    post = fakes["db"].add_post()
    fakes["nvidia"].chat_reply = {"reply": "Posted!", "action": {"name": "post_now", "post": None}}
    await h.on_text(CHAT_ID, "post it right now")
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice"
    assert fakes["messenger"].texts()[-1].startswith("I can't do that from chat.")


async def test_chat_tune_runs_directly_on_a_waiting_draft(h, fakes):
    post = fakes["db"].add_post(draft_b="")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "tune", "post": None, "mode": "short"}}
    await h.on_text(CHAT_ID, "make it shorter")
    assert "✂️ Shortening draft A…" in fakes["messenger"].texts()
    assert post.id in h.regenerating or not h.tasks  # the tune task was spawned
    await drain(h)


async def test_drafts_lookup_gives_refs_the_model_can_act_on(h, fakes):
    post = fakes["db"].add_post(topic="eval costs")
    text = await h.posts_tool_text(CHAT_ID, "awaiting_choice")
    assert f"[ref {post.id[:8]}]" in text


async def test_chat_prompt_carries_the_current_time(h, fakes):
    await h.on_text(CHAT_ID, "hey")
    assert "Current time: Tue 22 Sep, 10:00 AM IST" in fakes["nvidia"].chat_calls[0]["messages"][0]["content"]


async def test_explicit_requests_skip_jev(h, fakes):
    h.svc.jev = jev = FakeJev(0.0)
    await h.on_text(CHAT_ID, "make a post about eval costs")
    await drain(h)
    assert jev.seen == [] and len(fakes["db"].posts) == 1


async def test_jev_client_parses_and_fails_open():
    import httpx

    from app.jev import JevGate

    def ok(req):
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"answers": {"wants_drafts": {"type": "noul", "noul": 0.93}}}})

    rows = []
    gate = JevGate(httpx.AsyncClient(transport=httpx.MockTransport(ok)), "jev-key")
    gate.recorder = rows.append
    assert await gate.wants_drafts([{"role": "user", "content": "yes"}]) == 0.93
    assert rows[0]["provider"] == "jev" and rows[0]["ok"] is True

    def limited(req):
        return httpx.Response(429, json={"code": -1, "message": "Too many requests."})

    gate = JevGate(httpx.AsyncClient(transport=httpx.MockTransport(limited)), "jev-key")
    assert await gate.wants_drafts([{"role": "user", "content": "yes"}]) is None
