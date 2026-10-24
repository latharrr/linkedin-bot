"""Spend cap for the paid Groq account (GROQ_API_KEY_2, Developer plan, pay per token).

Today's spend (IST day) is estimated from the usage ledger: tokens × Groq's list
price. At the cap the paid link raises ApiError, so the writer chain and the
browser failover simply move on. A Groq console spend limit is the hard backstop.
"""

from __future__ import annotations

import time
from typing import Any

from app.apiclient import ApiError
from app.log import get_logger
from app.timeutil import ist_day_start, utcnow

log = get_logger(__name__)

# USD per 1M tokens (input, output), Groq on-demand, Sept 2026. Cached input is half
# price, so these are upper-bound estimates.
PRICES = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
DEFAULT_PRICE = (0.15, 0.60)
CACHE_SECONDS = 60.0  # re-read the ledger at most once a minute


def call_cost(row: dict[str, Any]) -> float:
    price_in, price_out = PRICES.get(str(row.get("model") or ""), DEFAULT_PRICE)
    return (int(row.get("prompt_tokens") or 0) * price_in + int(row.get("completion_tokens") or 0) * price_out) / 1_000_000


def spend(rows: list[dict[str, Any]], provider: str) -> float:
    return sum(call_cost(r) for r in rows if r.get("provider") == provider)


class SpendCap:
    """Wraps a paid client (chat + browse). Everything else passes through."""

    def __init__(self, inner: Any, db: Any, daily_usd: float, clock: Any = time.monotonic) -> None:
        self._inner = inner
        self._db = db
        self._daily = daily_usd
        self._clock = clock
        self._cached: tuple[float, float] | None = None  # (read at, spent)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def spent_today(self) -> float:
        now = self._clock()
        if self._cached and now - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        rows = await self._db.api_calls_since(ist_day_start(utcnow()))
        spent = spend(rows, self._inner.provider_name)
        self._cached = (now, spent)
        return spent

    async def _check(self) -> None:
        spent = await self.spent_today()
        if spent >= self._daily:
            log.warning("spend_cap_reached", extra={"provider": self._inner.provider_name, "spent_usd": round(spent, 4), "cap_usd": self._daily})
            raise ApiError(f"{self._inner.provider_name}: daily spend cap reached (${spent:.3f} of ${self._daily:.2f})")

    async def chat(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        await self._check()
        return await self._inner.chat(model, messages, **kwargs)

    async def browse(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        await self._check()
        return await self._inner.browse(model, messages, **kwargs)
