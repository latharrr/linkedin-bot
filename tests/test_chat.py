"""Conversation mode: post requests vs chat, the topic prompt, and the 8am-nudge reply."""

import asyncio
from datetime import UTC, datetime

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
    assert "Nothing is published until he taps ✅ Post this AND a posting time" in system


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
