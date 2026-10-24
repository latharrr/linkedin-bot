"""Regression tests for the Oct 24 review fixes: ask re-prompt on filler replies,
real question kept in chat memory, fallback pile-up cap, Telegram send retry."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
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
