"""Regression tests for the Oct 24 review fixes: ask re-prompt on filler replies,
real question kept in chat memory, fallback pile-up cap, Telegram send retry."""
import asyncio
import json as _json
from datetime import UTC, datetime, timedelta

import pytest
from telegram import Chat, Message, Update
from telegram.error import TimedOut

from app.chat import low_signal_answer
from app.telegram_ui import TelegramMessenger
from bot import FALLBACK_MAX_WAITING, REPROMPT, STAKE_NOTE, Ask, BotHandler, fallback
from tests.conftest import CHAT_ID

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)


@pytest.fixture
def h(svc):
    return BotHandler(svc, now=lambda: NOW)


async def drain(h: BotHandler) -> None:
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


async def _nosleep(_: float) -> None:
    return None


# ── low_signal_answer ───────────────────────────────────────────────────────
def test_low_signal_answer_flags_filler():
    for text in ("hey", "Hi!", "ok", "hmm", "test", "?", "yo"):
        assert low_signal_answer(text), text


def test_low_signal_answer_passes_real_answers():
    for text in ("we merged two streams in n8n last night", "it took 3 weeks", "GROQ-DL-2026"):
        assert not low_signal_answer(text), text


# ── ask re-prompt ────────────────────────────────────────────────────────────
async def test_filler_reply_to_ask_reprompts_and_keeps_ask(h, fakes):
    h.asks[CHAT_ID] = Ask("nonce1", "n8n merge node", STAKE_NOTE)
    await h.on_text(CHAT_ID, "hey")
    assert CHAT_ID in h.asks  # ask survives: his filler was not swallowed as the answer
    texts = fakes["messenger"].texts()
    assert texts and texts[-1] == REPROMPT
    assert not fakes["db"].posts  # no generation ran on "hey"


async def test_real_reply_to_ask_runs_generation(h, fakes):
    h.asks[CHAT_ID] = Ask("nonce1", "n8n merge node", STAKE_NOTE)
    await h.on_text(CHAT_ID, "the merge node silently dropped 4 of 200 records last night")
    await drain(h)
    assert CHAT_ID not in h.asks  # consumed
    assert fakes["db"].posts  # generation actually started


# ── ask_for_detail keeps the real question in memory ───────────────────────
class _Problems:
    def values(self):
        return [["invented a number"]]

    def text(self):
        return "- hook: invented a number"


async def test_grounding_hold_question_enters_chat_memory(h):
    await h.ask_for_detail(CHAT_ID, "my n8n merge incident", _Problems())
    history = h.memory.history(CHAT_ID)
    assert any("held this draft back" in m["content"] for m in history if m["role"] == "assistant")
    assert CHAT_ID in h.asks


# ── fallback pile-up cap ─────────────────────────────────────────────────────
async def test_fallback_skips_when_drafts_pile_up(svc, fakes):
    for _ in range(FALLBACK_MAX_WAITING):
        fakes["db"].add_post(chat_id=CHAT_ID, brief="old", status="awaiting_choice",
                             created_at=YESTERDAY)
    assert await fallback(svc, NOW) == "skipped_pile"
    assert len(fakes["db"].posts) == FALLBACK_MAX_WAITING  # nothing new added


# ── Telegram send retry ──────────────────────────────────────────────────────
class FlakyBot:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimedOut()
        return None


async def test_send_text_retries_transient_failures(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    bot = FlakyBot(2)
    await TelegramMessenger(bot).send_text(1, "hello")
    assert bot.calls == 3  # 2 failures + 1 success


async def test_send_text_gives_up_after_three_attempts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    bot = FlakyBot(5)
    with pytest.raises(TimedOut):
        await TelegramMessenger(bot).send_text(1, "hello")
    assert bot.calls == 3


async def test_keyboard_send_never_retries_a_timeout(monkeypatch):
    """A TimedOut means the message may have landed: never double a confirmation."""
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    bot = FlakyBot(5)
    with pytest.raises(TimedOut):
        await TelegramMessenger(bot).send_text(1, "confirm?", keyboard=[[("✅", "x:1")]])
    assert bot.calls == 1  # one attempt only


async def test_keyboard_send_still_retries_network_error(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    class GatewayBot(FlakyBot):
        async def send_message(self, **kwargs):
            from telegram.error import NetworkError
            self.calls += 1
            if self.calls <= self.failures:
                raise NetworkError("Bad Gateway")
            return None

    bot = GatewayBot(2)
    await TelegramMessenger(bot).send_text(1, "confirm?", keyboard=[[("✅", "x:1")]])
    assert bot.calls == 3


async def test_photo_send_never_retries_a_timeout(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    class PhotoBot(FlakyBot):
        async def send_photo(self, **kwargs):
            self.calls += 1
            raise TimedOut()

    bot = PhotoBot(5)
    with pytest.raises(TimedOut):
        await TelegramMessenger(bot).send_photo(1, "https://example.com/x.png")
    assert bot.calls == 1


# ── pending-interaction persistence ─────────────────────────────────────────
async def test_interactions_saved_after_update(h, fakes):
    h.asks[CHAT_ID] = Ask("nonce1", "n8n merge node", STAKE_NOTE)
    msg = Message(message_id=1, date=NOW, chat=Chat(id=CHAT_ID, type="private"), text="hey")
    await h.on_update(Update(update_id=3, message=msg))  # finally-block persists interactions
    raw = fakes["db"].settings.get(f"interaction:{CHAT_ID}")
    assert raw, "interactions not persisted"
    snap = _json.loads(raw)
    assert snap["ask"]["nonce"] == "nonce1"
    assert snap["ask"]["brief"] == "n8n merge node"


async def test_restore_interactions_roundtrip(svc, fakes):
    h1 = BotHandler(svc, now=lambda: NOW)
    h1.asks[CHAT_ID] = Ask("n1", "brief b", STAKE_NOTE, thing="code", reprompt="again")
    h1.offers[CHAT_ID] = ("n2", "topic t")
    h1.choosing[CHAT_ID] = ("n3", {"name": "schedule"}, ["p1", "p2"])
    await h1.save_interactions(CHAT_ID)
    h2 = BotHandler(svc, now=lambda: NOW)  # fresh process memory
    assert not h2.asks and not h2.offers and not h2.choosing
    await h2.restore_interactions()
    assert h2.asks[CHAT_ID].nonce == "n1"
    assert h2.asks[CHAT_ID].thing == "code"
    assert h2.offers[CHAT_ID] == ("n2", "topic t")
    assert h2.choosing[CHAT_ID] == ("n3", {"name": "schedule"}, ["p1", "p2"])


async def test_expired_interactions_not_restored(svc, fakes):
    old = NOW - timedelta(hours=2)
    fakes["db"].settings[f"interaction:{CHAT_ID}"] = _json.dumps({
        "saved_at": old.isoformat(),
        "ask": {"nonce": "n1", "brief": "b", "note": "n", "thing": "", "offer_no_story": True, "force": False, "reprompt": ""},
        "pending": None, "choosing": None, "offer": None,
    })
    h2 = BotHandler(svc, now=lambda: NOW)
    await h2.restore_interactions()
    assert CHAT_ID not in h2.asks
