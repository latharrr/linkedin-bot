"""The paid Groq account's daily spend cap."""

import pytest

from app.apiclient import ApiError
from app.spend import SpendCap, call_cost, spend
from app.usage import groq_card


def test_call_cost_uses_list_prices():
    row = {"provider": "groq2", "model": "openai/gpt-oss-120b", "prompt_tokens": 1_000_000, "completion_tokens": 100_000}
    assert call_cost(row) == pytest.approx(0.15 + 0.06)
    assert spend([row, {**row, "provider": "groq"}], "groq2") == pytest.approx(0.21)  # the free account costs nothing


class Ledger:
    def __init__(self, rows):
        self.rows = rows
        self.reads = 0

    async def api_calls_since(self, since):
        self.reads += 1
        return list(self.rows)


class Paid:
    provider_name = "groq2"

    def __init__(self):
        self.calls = 0

    async def chat(self, model, messages, **kw):
        self.calls += 1
        return "ok"

    async def browse(self, model, messages, **kw):
        self.calls += 1
        return "page"


def row(tokens):
    return {"provider": "groq2", "model": "openai/gpt-oss-120b", "prompt_tokens": tokens, "completion_tokens": 0}


async def test_under_the_cap_calls_go_through():
    paid = Paid()
    cap = SpendCap(paid, Ledger([row(100_000)]), daily_usd=0.25)  # $0.015 spent
    assert await cap.chat("m", []) == "ok" and await cap.browse("m", []) == "page" and paid.calls == 2


async def test_at_the_cap_the_link_fails_fast_so_the_chain_moves_on():
    paid = Paid()
    cap = SpendCap(paid, Ledger([row(2_000_000)]), daily_usd=0.25)  # $0.30 spent
    with pytest.raises(ApiError, match="daily spend cap"):
        await cap.chat("m", [])
    assert paid.calls == 0


async def test_ledger_read_is_cached_for_a_minute():
    now = [0.0]
    ledger = Ledger([])
    cap = SpendCap(Paid(), ledger, daily_usd=1.0, clock=lambda: now[0])
    await cap.chat("m", [])
    await cap.chat("m", [])
    now[0] = 61.0
    await cap.chat("m", [])
    assert ledger.reads == 2


def test_capped_client_passes_attributes_through():
    paid = Paid()
    cap = SpendCap(paid, Ledger([]), daily_usd=1.0)
    cap.__dict__  # noqa: B018
    assert cap.provider_name == "groq2"


def test_paid_card_shows_money_not_tokens():
    c = groq_card(True, None, "groq2", "Groq (paid account)", spent_usd=0.0123, cap_usd=0.25)
    assert c["meters"][0]["label"] == "Spend, last 24 h (est.)" and c["meters"][0]["limit"] == 0.25


def test_money_is_formatted_in_dollars_and_cents():
    from app.usage import meter_line

    line = meter_line({"label": "Spend, last 24 h (est.)", "used": 0.0123, "limit": 0.25, "unit": "USD"})
    assert line == "Spend, last 24 h (est.): $0.012 of $0.25 cap · $0.24 left"
