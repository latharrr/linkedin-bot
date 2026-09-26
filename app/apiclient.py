"""Shared JSON-over-HTTPS plumbing for the model providers (NVIDIA NIM, Groq).

Bearer auth, a timeout on every call, and retry on 429 / 5xx / connect errors
(both free tiers are rate-limited). Read timeouts are not retried: a slow
generation would just run twice.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.log import get_logger

log = get_logger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF = 30.0
MAX_MINUTE_WAIT = 65.0  # default cap; a per-minute window always clears within a minute
_PER_MINUTE = re.compile(r"per minute|\bTPM\b|\bRPM\b", re.I)

# Reasoning models may prepend their chain of thought in <think> tags.
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_DANGLING_THINK = re.compile(r"^.*?</think>", re.S | re.I)


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ChatResult:
    text: str
    finish_reason: str | None


def strip_think(text: str) -> str:
    text = _THINK.sub("", text)
    if "</think>" in text.lower():  # opening tag was cut off / omitted
        text = _DANGLING_THINK.sub("", text, count=1)
    return text.strip()


def _per_minute(resp: httpx.Response) -> bool:
    return resp.status_code == 429 and bool(_PER_MINUTE.search(resp.text))


def _retry_after(resp: httpx.Response, attempt: int, minute_wait_cap: float = MAX_MINUTE_WAIT) -> float:
    cap = minute_wait_cap if _per_minute(resp) else MAX_BACKOFF
    try:
        return min(float(resp.headers.get("retry-after", "")), cap)
    except ValueError:
        return min(2.0 * 2**attempt, cap)


def _long_wait(resp: httpx.Response) -> bool:
    """A 429 asking for more than MAX_BACKOFF is a quota (e.g. Groq's tokens-per-DAY
    limit): fail now so a fallback can take over. A per-MINUTE limit is waited out."""
    try:
        long = resp.status_code == 429 and float(resp.headers.get("retry-after", "0")) > MAX_BACKOFF
    except ValueError:
        return False
    return long and not _per_minute(resp)


Recorder = Callable[[dict[str, Any]], None]


class ApiClient:
    error_cls: type[ApiError] = ApiError
    provider_name = "api"
    recorder: Recorder | None = None  # set by build_services → every HTTP attempt lands in the usage ledger

    def __init__(
        self,
        api_key: str,
        http: httpx.AsyncClient,
        *,
        max_attempts: int = 3,
        minute_wait_cap: float = MAX_MINUTE_WAIT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key = api_key
        self._http = http
        self._max_attempts = max_attempts
        self._minute_wait_cap = minute_wait_cap
        self._sleep = sleep

    async def _post(self, url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        what = url.rsplit("/", 1)[-1]
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            try:
                resp = await self._http.post(url, json=body, headers=headers, timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                self._record(what, body, None, exc)
                if last:
                    raise self.error_cls(f"{what}: {type(exc).__name__}") from exc
                await self._sleep(min(2.0 * 2**attempt, MAX_BACKOFF))
                continue
            except httpx.HTTPError as exc:
                self._record(what, body, None, exc)
                raise self.error_cls(f"{what}: {type(exc).__name__}") from exc
            self._record(what, body, resp)
            if resp.status_code in RETRY_STATUS and not last and not _long_wait(resp):
                log.warning("api_retry", extra={"endpoint": what, "status": resp.status_code, "attempt": attempt + 1})
                await self._sleep(_retry_after(resp, attempt, self._minute_wait_cap))
                continue
            if resp.status_code >= 400:
                raise self.error_cls(f"{what}: HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code)
            try:
                return resp.json()
            except ValueError as exc:
                raise self.error_cls(f"{what}: response is not JSON") from exc
        raise self.error_cls(f"{what}: retries exhausted")  # pragma: no cover - loop always returns/raises

    def _record(self, what: str, body: dict[str, Any], resp: httpx.Response | None, exc: BaseException | None = None) -> None:
        if self.recorder is None:
            return
        from app.usage import call_row  # local import: usage imports nothing from here

        try:
            self.recorder(call_row(self.provider_name, what, body.get("model") or what, resp, exc))
        except Exception:  # recording must never break a call
            log.warning("usage_record_skipped", extra={"endpoint": what})

    def _parse_chat(self, data: dict[str, Any], model: str) -> tuple[ChatResult, dict[str, Any]]:
        """OpenAI-schema chat response → (ChatResult, raw message)."""
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise self.error_cls("chat: unexpected response shape") from exc
        usage = data.get("usage") or {}
        log.info("chat_call", extra={"provider": type(self).__name__, "model": model, "out_tokens": usage.get("completion_tokens")})
        return ChatResult(strip_think(content), choice.get("finish_reason")), message
