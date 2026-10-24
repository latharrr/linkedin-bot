"""Free-model chat providers (OpenRouter, ModelScope, ZenMux) and the writer fallback chain.

Free capacity is shared: at any moment a model may return 429 ("temporarily
rate-limited upstream"), 403, 401 or an upstream error. A failure therefore moves
to the next model in the chain at once instead of waiting (max_attempts=1).

Reasoning is disabled per provider: with it on, most free OpenRouter models spent
the whole token budget thinking and returned 0 words (probed 2026-09-23), and
ModelScope's Qwen3 models require enable_thinking=false for non-streaming calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.apiclient import ApiClient, ApiError, ChatResult
from app.log import get_logger

log = get_logger(__name__)

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELSCOPE_URL = "https://api-inference.modelscope.ai/v1"  # international site (keys start with ms-)
ZENMUX_URL = "https://zenmux.ai/api/v1/chat/completions"


class OpenRouterError(ApiError):
    pass


class ModelScopeError(ApiError):
    pass


class ZenMuxError(ApiError):
    pass


class CompatChatClient(ApiClient):
    """Any OpenAI-compatible /chat/completions endpoint, plus provider-specific body fields."""

    extra_body: dict[str, Any] = {}

    def __init__(self, api_key: str, http: Any, *, chat_timeout: float, url: str, **kwargs: Any) -> None:
        kwargs.setdefault("max_attempts", 1)
        super().__init__(api_key, http, **kwargs)
        self._chat_timeout = chat_timeout
        self._url = url

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
        body.update(self.extra_body)
        if temperature is not None:
            body["temperature"] = temperature
        data = await self._post(self._url, body, self._chat_timeout)
        if "choices" not in data and data.get("error"):  # some gateways report upstream failures in a 200 body
            raise self.error_cls(f"completions: upstream error: {str(data['error'])[:200]}")
        return self._parse_chat(data, model)[0]


class ModelScopeClient(CompatChatClient):
    error_cls = ModelScopeError
    provider_name = "modelscope"
    extra_body = {"enable_thinking": False}

    def __init__(self, api_key: str, http: Any, *, chat_timeout: float, base_url: str = MODELSCOPE_URL, **kwargs: Any) -> None:
        super().__init__(api_key, http, chat_timeout=chat_timeout, url=f"{base_url.rstrip('/')}/chat/completions", **kwargs)


class OpenRouterClient(CompatChatClient):
    error_cls = OpenRouterError
    provider_name = "openrouter"
    extra_body = {"reasoning": {"enabled": False}}

    def __init__(self, api_key: str, http: Any, *, chat_timeout: float, **kwargs: Any) -> None:
        super().__init__(api_key, http, chat_timeout=chat_timeout, url=CHAT_URL, **kwargs)


class ZenMuxClient(CompatChatClient):
    error_cls = ZenMuxError
    provider_name = "zenmux"
    extra_body = {"reasoning": {"enabled": False}}  # ZenMux mirrors OpenRouter's request schema

    def __init__(self, api_key: str, http: Any, *, chat_timeout: float, **kwargs: Any) -> None:
        super().__init__(api_key, http, chat_timeout=chat_timeout, url=ZENMUX_URL, **kwargs)


class ChatClient(Protocol):
    async def chat(
        self, model: str, messages: list[dict[str, str]], *, temperature: float | None = None, max_tokens: int = 4096
    ) -> ChatResult: ...


@dataclass(frozen=True)
class ChainLink:
    provider: str
    client: ChatClient
    model: str


class FallbackChat:
    """Tries each (client, model) in order. A link fails on any API error, an
    empty reply, or a reply cut off by max_tokens; the next link then gets the
    same request. The `model` argument from Writer is ignored — the chain decides."""

    def __init__(self, links: list[ChainLink]) -> None:
        if not links:
            raise ValueError("fallback chain is empty")
        self.links = links
        self.last_served: str | None = None

    async def chat(
        self, model: str, messages: list[dict[str, str]], *, temperature: float | None = None, max_tokens: int = 4096
    ) -> ChatResult:
        failures: list[str] = []
        for link in self.links:
            label = f"{link.provider}:{link.model}"
            try:
                result = await link.client.chat(link.model, messages, temperature=temperature, max_tokens=max_tokens)
            except ApiError as exc:
                failures.append(f"{label}: {str(exc)[:120]}")
                continue
            if not result.text or result.finish_reason == "length":
                failures.append(f"{label}: {'empty reply' if not result.text else 'cut off by max_tokens'}")
                continue
            if failures:
                log.warning("writer_fallback_used", extra={"served_by": label, "skipped": failures})
            self.last_served = label
            return result
        raise ApiError("every writer in the fallback chain failed: " + " | ".join(failures))


def parse_chain(spec: str) -> list[tuple[str, str]]:
    """'groq:openai/gpt-oss-120b, openrouter:qwen/qwen3.8-27b:free' → [(provider, model), …].
    Splits on the FIRST colon only, since OpenRouter model ids contain ':free'."""
    entries = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        provider, sep, model = raw.partition(":")
        if not sep or not model:
            raise ValueError(f"bad writer chain entry {raw!r}; expected provider:model")
        entries.append((provider.strip(), model.strip()))
    return entries
