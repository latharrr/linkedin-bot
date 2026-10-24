"""NVIDIA NIM client: chat completions, embeddings, and FLUX.1-dev images.
Transport, retries and error handling live in app/apiclient.py."""

from __future__ import annotations

import base64
from typing import Any, Literal

from app.apiclient import (  # noqa: F401 - re-exported
    ApiClient,
    ApiError,
    ChatResult,
    strip_think,
)

CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
FLUX_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
# Native ~4:5 at 50 steps (probed 2026-09-23: ~16 s). The default was a 1024² square that
# was cropped and upscaled to 1080x1350; this needs almost no crop and no upscaling.
FLUX_SIZE = (1088, 1344)
FLUX_STEPS = 50
FLUX_CFG = 3.5

InputType = Literal["query", "passage"]
# NIM's reasoning chat models (deepseek-v4.1-flash, nemotron-3-super, …) think by default:
# de-AI calls then took >400 s or burned all max_tokens on reasoning, and with prompt-level
# toggles the reasoning leaked into `content` untagged. Turning it off at the template level
# fixes both. Each model reads its own key and ignores the other (verified 2026-09-23).
THINKING_OFF = {"thinking": False, "enable_thinking": False}


class NvidiaError(ApiError):
    pass


class NvidiaClient(ApiClient):
    error_cls = NvidiaError
    provider_name = "nvidia"

    def __init__(
        self,
        api_key: str,
        http: Any,
        *,
        chat_timeout: float,
        embed_timeout: float,
        image_timeout: float,
        chat_template_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, http, **kwargs)
        self._chat_timeout = chat_timeout
        self._embed_timeout = embed_timeout
        self._image_timeout = image_timeout
        self._template_kwargs = chat_template_kwargs

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
        if temperature is not None:
            body["temperature"] = temperature
        if self._template_kwargs:
            body["chat_template_kwargs"] = self._template_kwargs
        return self._parse_chat(await self._post(CHAT_URL, body, self._chat_timeout), model)[0]

    async def embed(self, model: str, texts: list[str], input_type: InputType) -> list[list[float]]:
        body = {
            "model": model,
            "input": texts,
            "input_type": input_type,  # asymmetric retrieval models need query vs passage
            "encoding_format": "float",
            "truncate": "END",  # long posts exceed the model's token window; keep the start
        }
        data = await self._post(EMBED_URL, body, self._embed_timeout)
        try:
            rows = sorted(data["data"], key=lambda d: d["index"])
            vectors = [list(map(float, r["embedding"])) for r in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise NvidiaError("embeddings: unexpected response shape") from exc
        if len(vectors) != len(texts):
            raise NvidiaError(f"embeddings: sent {len(texts)} texts, got {len(vectors)} vectors")
        return vectors

    async def flux(self, prompt: str) -> bytes:
        """FLUX.1-dev → decoded image bytes (JPEG)."""
        body = {"prompt": prompt, "mode": "base", "width": FLUX_SIZE[0], "height": FLUX_SIZE[1], "steps": FLUX_STEPS, "cfg_scale": FLUX_CFG}
        data = await self._post(FLUX_URL, body, self._image_timeout)
        try:
            artifact = data["artifacts"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise NvidiaError("flux: response has no artifacts") from exc
        if artifact.get("finishReason") != "SUCCESS":
            raise NvidiaError(f"flux: finishReason={artifact.get('finishReason')!r}")
        try:
            return base64.b64decode(artifact["base64"], validate=True)
        except (KeyError, ValueError) as exc:
            raise NvidiaError("flux: artifact base64 missing or invalid") from exc
