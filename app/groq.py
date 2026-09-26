"""Groq client: OpenAI-compatible chat (the writer) and chat + built-in
`browser_search` (the researcher). Transport and retries: app/apiclient.py.

gpt-oss models reason by default; `reasoning_effort` + `include_reasoning=false`
keep reasoning short and out of the response (verified 2026-09-23).

Free tier: gpt-oss-120b allows 8,000 tokens/minute and a writing call is
~1.2–2.5k tokens, so parallel calls trip 429s. Calls are serialised (Groq
answers in ~1 s, so this costs little). A 429 gets ONE short retry (a few
seconds, not a full per-minute window) — then it's the fallback chain's job
(app/openrouter.FallbackChat, app/research.FailoverBrowser) to move on to
GROQ_API_KEY_2 or the next provider rather than blocking here. This client used
to wait out full per-minute windows across up to 6 attempts; that made every
busy pipeline run stall for minutes before the paid fallback account ever got a
turn (see fallback.log 2026-09-26 08:32–08:39 for a live example: ~7 minutes
for one draft, almost all of it spent sleeping on 429s it eventually beat
anyway). NVIDIA (app/nvidia.py) keeps the old patient behaviour: it sits at the
tail of the chain or runs standalone, so there's nothing better to fail over
to.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.apiclient import ApiClient, ApiError, ChatResult

CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqError(ApiError):
    pass


@dataclass(frozen=True)
class BrowseResult:
    text: str
    finish_reason: str | None
    # URL → raw text of every page/snippet the model actually opened (not its summary)
    pages: dict[str, str] = field(default_factory=dict)


def page_evidence(message: dict[str, Any]) -> dict[str, str]:
    """Collect raw page text from `executed_tools`. Search-result stubs have empty
    content and are skipped, so only pages the model opened count as evidence."""
    pages: dict[str, str] = {}
    for tool in message.get("executed_tools") or []:
        results = (tool.get("search_results") or {}).get("results") or []
        for r in results:
            url, content = r.get("url"), r.get("content") or ""
            if url and content.strip():
                pages[url] = (pages.get(url, "") + "\n" + content).strip()
    return pages


def reasoning_effort_for(model: str, configured: str) -> str | None:
    """gpt-oss takes low/medium/high; Qwen takes none/default ("low" made it spend the
    whole reply on hidden reasoning and return nothing — probed 2026-09-23)."""
    if "gpt-oss" in model:
        return configured
    if model.startswith("qwen/"):
        return "none"
    return None


class GroqClient(ApiClient):
    error_cls = GroqError
    provider_name = "groq"

    def __init__(
        self,
        api_key: str,
        http: Any,
        *,
        chat_timeout: float,
        browse_timeout: float,
        reasoning_effort: str = "low",
        max_concurrency: int = 1,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("max_attempts", 2)  # one retry, then let the fallback chain take over
        kwargs.setdefault("minute_wait_cap", 8.0)  # short courtesy wait, not a full minute
        super().__init__(api_key, http, **kwargs)
        self._gate = asyncio.Semaphore(max_concurrency)
        self._chat_timeout = chat_timeout
        self._browse_timeout = browse_timeout
        self._effort = reasoning_effort

    def _body(self, model: str, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "include_reasoning": False}
        effort = reasoning_effort_for(model, self._effort)
        if effort is not None:
            body["reasoning_effort"] = effort
        return body

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        body = self._body(model, messages, max_tokens)
        if temperature is not None:
            body["temperature"] = temperature
        async with self._gate:
            data = await self._post(CHAT_URL, body, self._chat_timeout)
        return self._parse_chat(data, model)[0]

    async def browse(self, model: str, messages: list[dict[str, str]], *, max_tokens: int = 4096) -> BrowseResult:
        body = self._body(model, messages, max_tokens) | {
            "tools": [{"type": "browser_search"}],
            "tool_choice": "required",
        }
        async with self._gate:
            data = await self._post(CHAT_URL, body, self._browse_timeout)
        result, message = self._parse_chat(data, model)
        return BrowseResult(result.text, result.finish_reason, page_evidence(message))
