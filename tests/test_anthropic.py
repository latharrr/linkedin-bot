import json

import httpx
import pytest

from app.anthropic import MESSAGES_URL, AnthropicClient, AnthropicError


async def _nosleep(_seconds: float) -> None:
    pass


def recorder(response):
    seen = []

    def handler(req):
        seen.append((str(req.url), json.loads(req.content), dict(req.headers)))
        return response(req) if callable(response) else response

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(text="Clean post.", stop="end_turn"):
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


async def test_sends_anthropic_shape_and_parses_reply():
    seen, http = recorder(ok())
    client = AnthropicClient("sk-ant-key", http, chat_timeout=5)
    result = await client.chat(
        "claude-haiku-5-5",
        [{"role": "system", "content": "voice"}, {"role": "user", "content": "brief"}],
        temperature=0.85,
        max_tokens=1200,
    )
    url, body, headers = seen[0]
    assert url == MESSAGES_URL
    assert headers["x-api-key"] == "sk-ant-key" and headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers  # Anthropic uses x-api-key, not Bearer
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"] == "voice"  # the system prompt rides as a field, not a message
    assert body["messages"] == [{"role": "user", "content": "brief"}]
    assert body["max_tokens"] == 1200 and body["temperature"] == 0.85
    assert result.text == "Clean post." and result.finish_reason == "stop"


async def test_system_messages_join_and_drop_out_of_turns():
    seen, http = recorder(ok())
    await AnthropicClient("k", http, chat_timeout=5).chat(
        "m",
        [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "r1"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "q2"},
        ],
    )
    body = seen[0][1]
    assert body["system"] == "a\n\nb"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]


async def test_max_tokens_stop_maps_to_length_so_the_chain_moves_on():
    _seen, http = recorder(ok("half a post", stop="max_tokens"))
    result = await AnthropicClient("k", http, chat_timeout=5).chat("m", [{"role": "user", "content": "q"}])
    assert result.finish_reason == "length"


async def test_api_error_raises_anthropic_error_without_retry():
    seen, http = recorder(httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(AnthropicError, match="HTTP 401"):
        await AnthropicClient("bad-key", http, chat_timeout=5).chat("m", [])
    assert len(seen) == 1


async def test_overloaded_529_is_retried_then_serves():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(529, json={"error": {"message": "overloaded"}}) if len(calls) == 1 else ok()

    _seen, http = recorder(handler)
    result = await AnthropicClient("k", http, chat_timeout=5, sleep=_nosleep).chat("m", [])
    assert result.text == "Clean post." and len(calls) == 2
