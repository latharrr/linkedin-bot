"""Anthropic (Claude) writer client: the strong paid writer at the head of the chain.

The Messages API differs from the OpenAI-compatible providers on three axes:
x-api-key + anthropic-version headers instead of Bearer auth, the system prompt is a
top-level field (not a message), and the reply is a list of content blocks with a
stop_reason, not choices. Stop reasons map onto the chain's convention
(app/openrouter.FallbackChat): "max_tokens" -> "length" so a cut-off reply fails over
to the next link, anything else -> "stop".
"""

from __future__ import annotations

import os
from typing import Any

from app.apiclient import ApiClient, ApiError, ChatResult
from app.log import get_logger

log = get_logger(__name__)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicError(ApiError):
    pass


class AnthropicClient(ApiClient):
    error_cls = AnthropicError
    provider_name = "anthropic"

    def __init__(self, api_key: str, http: Any, *, chat_timeout: float, **kwargs: Any) -> None:
        kwargs.setdefault("max_attempts", 2)  # one retry, then the fallback chain takes over
        super().__init__(api_key, http, **kwargs)
        self._chat_timeout = chat_timeout

    def _auth_headers(self) -> dict[str, str]:
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION}
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        if workspace:  # a key that is not scoped to one workspace must name it (HTTP 400 otherwise)
            headers["anthropic-workspace-id"] = workspace
        return headers

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        body: dict[str, Any] = {"model": model, "messages": turns, "max_tokens": max_tokens}
        if system:
            body["system"] = system
        # Haiku 5.5 rejects temperature/top_p/top_k (HTTP 400), so sampling params are never sent.
        data = await self._post(MESSAGES_URL, body, self._chat_timeout)
        try:
            text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
        except (AttributeError, KeyError, TypeError) as exc:
            raise self.error_cls("messages: unexpected response shape") from exc
        stop = data.get("stop_reason")
        usage = data.get("usage") or {}
        log.info("chat_call", extra={"provider": type(self).__name__, "model": model, "out_tokens": usage.get("output_tokens")})
        return ChatResult(text.strip(), "length" if stop == "max_tokens" else "stop")
