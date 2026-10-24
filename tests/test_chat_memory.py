"""Chat history persistence: snapshots survive a bot restart."""

import json

from app.chat import HISTORY_TURNS, ChatMemory
from bot import BotHandler
from tests.conftest import CHAT_ID


def test_snapshot_load_roundtrip():
    m = ChatMemory()
    m.add(1, "user", "draft that")
    m.add(1, "assistant", "on it")
    m2 = ChatMemory()
    assert m2.load(1, m.snapshot(1))
    assert m2.history(1) == [{"role": "user", "content": "draft that"}, {"role": "assistant", "content": "on it"}]


def test_load_rejects_garbage():
    m = ChatMemory()
    assert not m.load(1, "not json")
    assert not m.load(1, '{"a": 1}')
    assert not m.load(1, '[{"role": "hacker", "content": "x"}]')
    assert not m.load(1, "[]")
    assert not m.has_context(1)


def test_window_capped_on_load():
    m = ChatMemory()
    raw = json.dumps([{"role": "user", "content": str(i)} for i in range(HISTORY_TURNS + 5)])
    assert m.load(1, raw)
    assert len(m.history(1)) == HISTORY_TURNS
    assert m.history(1)[0]["content"] == "5"


def test_add_marks_dirty_and_mark_saved_clears():
    m = ChatMemory()
    assert m.dirty_chats() == []
    m.add(1, "user", "hi")
    assert m.dirty_chats() == [1]
    m.mark_saved(1)
    assert m.dirty_chats() == []


def test_load_is_not_dirty():
    m = ChatMemory()
    assert m.load(1, '[{"role": "user", "content": "hi"}]')
    assert m.dirty_chats() == []


async def test_flush_then_restore_across_restart(svc, fakes):
    bot = BotHandler(svc)
    bot.memory.add(CHAT_ID, "user", "draft that")
    await bot.flush_memory(CHAT_ID)
    assert fakes["db"].settings[f"chat_history:{CHAT_ID}"]
    assert bot.memory.dirty_chats() == []

    bot2 = BotHandler(svc)  # restart over the same db
    assert not bot2.memory.has_context(CHAT_ID)
    await bot2.restore_memory()
    assert bot2.memory.history(CHAT_ID) == [{"role": "user", "content": "draft that"}]


async def test_flush_skips_clean_chats(svc, fakes):
    bot = BotHandler(svc)
    await bot.flush_memory(CHAT_ID)
    assert fakes["db"].settings == {}
