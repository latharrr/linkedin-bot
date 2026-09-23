import json

import httpx
import pytest
from pydantic import ValidationError

from app.apiclient import ApiError, ChatResult
from app.config import DEFAULT_WRITER_FALLBACKS, Settings
from app.openrouter import (
    ChainLink,
    FallbackChat,
    ModelScopeClient,
    OpenRouterClient,
    OpenRouterError,
    ZenMuxClient,
    parse_chain,
)
from app.services import build_writer, writer_links
from app.writer import Writer


def recorder(response):
    seen = []

    def handler(req):
        seen.append((str(req.url), json.loads(req.content), req.headers["authorization"]))
        return response(req) if callable(response) else response

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(text="Clean post.\nWhat do you think?"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


# ── provider request shapes ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "cls,kwargs,url,extra",
    [
        (OpenRouterClient, {}, "https://openrouter.ai/api/v1/chat/completions", {"reasoning": {"enabled": False}}),
        (ModelScopeClient, {}, "https://api-inference.modelscope.ai/v1/chat/completions", {"enable_thinking": False}),
        (ModelScopeClient, {"base_url": "https://api-inference.modelscope.cn/v1/"}, "https://api-inference.modelscope.cn/v1/chat/completions", {"enable_thinking": False}),
        (ZenMuxClient, {}, "https://zenmux.ai/api/v1/chat/completions", {"reasoning": {"enabled": False}}),
    ],
)
async def test_compat_clients_send_reasoning_off(cls, kwargs, url, extra):
    seen, http = recorder(ok())
    result = await cls("key-123", http, chat_timeout=5, **kwargs).chat("m/x:free", [{"role": "user", "content": "q"}], temperature=0.85)
    sent_url, body, auth = seen[0]
    assert sent_url == url and auth == "Bearer key-123"
    assert body["model"] == "m/x:free" and body["temperature"] == 0.85 and body["stream"] is False
    for k, v in extra.items():
        assert body[k] == v
    assert result.text == "Clean post.\nWhat do you think?"


async def test_free_model_errors_fail_fast_without_retry():
    seen, http = recorder(httpx.Response(429, json={"error": {"message": "temporarily rate-limited upstream"}}))
    with pytest.raises(OpenRouterError, match="HTTP 429"):
        await OpenRouterClient("k", http, chat_timeout=5).chat("m", [])
    assert len(seen) == 1  # max_attempts=1: the chain moves on instead of waiting


async def test_upstream_error_in_200_body_is_an_error():
    body = {"id": "gen-1", "error": {"message": "Upstream error from Nvidia", "code": 502}}
    _seen, http = recorder(httpx.Response(200, json=body))
    with pytest.raises(OpenRouterError, match="upstream error"):
        await OpenRouterClient("k", http, chat_timeout=5).chat("m", [])


# ── fallback chain ───────────────────────────────────────────────────────────
class Scripted:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    async def chat(self, model, messages, *, temperature=None, max_tokens=4096):
        self.calls.append((model, temperature, max_tokens))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_chain_falls_through_errors_empty_and_truncated_replies():
    quota = Scripted(ApiError("HTTP 429: tokens per day"))
    empty = Scripted(ChatResult("", "stop"))
    cut = Scripted(ChatResult("half a post", "length"))
    good = Scripted(ChatResult("Final post.", "stop"))
    never = Scripted(ChatResult("unused", "stop"))
    chain = FallbackChat([
        ChainLink("groq", quota, "openai/gpt-oss-120b"),
        ChainLink("openrouter", empty, "a:free"),
        ChainLink("openrouter", cut, "b:free"),
        ChainLink("modelscope", good, "deepseek-ai/DeepSeek-V4-Pro"),
        ChainLink("nvidia", never, "n"),
    ])
    result = await chain.chat("ignored", [{"role": "user", "content": "q"}], temperature=0.85, max_tokens=1000)
    assert result.text == "Final post."
    assert chain.last_served == "modelscope:deepseek-ai/DeepSeek-V4-Pro"
    assert good.calls == [("deepseek-ai/DeepSeek-V4-Pro", 0.85, 1000)]  # each link uses its own model
    assert never.calls == []


async def test_chain_primary_success_touches_nothing_else():
    first, second = Scripted(ChatResult("ok", "stop")), Scripted(ChatResult("x", "stop"))
    chain = FallbackChat([ChainLink("groq", first, "g"), ChainLink("openrouter", second, "o")])
    await chain.chat("m", [])
    assert len(first.calls) == 1 and second.calls == []


async def test_chain_all_fail_raises_with_every_reason():
    chain = FallbackChat([ChainLink("groq", Scripted(ApiError("quota")), "g"), ChainLink("zenmux", Scripted(ApiError("403 payg")), "z")])
    with pytest.raises(ApiError, match="groq:g: quota.*zenmux:z: 403 payg"):
        await chain.chat("m", [])


async def test_writer_error_surface_when_chain_exhausted():
    chain = FallbackChat([ChainLink("groq", Scripted(ApiError("down")), "g")])
    with pytest.raises(ApiError):
        await Writer(chain, "chain", 0.85).deai("d", "R", {"current_role": "x"})


def test_empty_chain_rejected():
    with pytest.raises(ValueError):
        FallbackChat([])


# ── chain spec parsing & wiring ──────────────────────────────────────────────
def test_parse_chain_splits_on_first_colon_only():
    assert parse_chain(" openrouter:qwen/qwen3.8-27b:free , groq:openai/gpt-oss-120b,, ") == [
        ("openrouter", "qwen/qwen3.8-27b:free"),
        ("groq", "openai/gpt-oss-120b"),
    ]
    assert parse_chain("") == []
    with pytest.raises(ValueError):
        parse_chain("no-colon-here")


def test_default_chain_covers_every_free_provider_in_probe_order():
    chain = parse_chain(DEFAULT_WRITER_FALLBACKS)
    providers = [p for p, _ in chain]
    # Paid account's best model, then the free account's per-model quotas, then OpenRouter.
    assert chain[:3] == [("groq2", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b"), ("groq", "qwen/qwen3.8-27b")]
    assert ("groq2", "qwen/qwen3.8-27b") not in chain  # never pay for a weaker model
    assert providers[3:6] == ["openrouter"] * 3
    assert {"groq", "groq2", "openrouter", "modelscope", "zenmux", "nvidia"} == set(providers)
    assert providers[-1] == "nvidia"  # paid-credit NVIDIA is the last resort
    assert ("openrouter", "openrouter/free") in parse_chain(DEFAULT_WRITER_FALLBACKS)


BASE = dict(
    telegram_bot_token="t", my_chat_id=1, supabase_url="https://x.supabase.co", supabase_service_key="s",
    nvidia_api_key="n", linkedin_api_version="202601", groq_api_key="g",
)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **{**BASE, **kw})


def test_links_skip_providers_without_keys_and_dedupe():
    clients = {"groq": object(), "openrouter": object(), "nvidia": object()}  # no modelscope / zenmux key
    links = writer_links(settings(), clients)
    assert (links[0].provider, links[0].model) == ("groq", "openai/gpt-oss-120b")
    assert {link.provider for link in links} == {"groq", "openrouter", "nvidia"}
    labels = [(link.provider, link.model) for link in links]
    assert len(labels) == len(set(labels))


def test_primary_is_not_repeated_when_also_in_fallbacks():
    s = settings(writer_provider="openrouter", openrouter_api_key="o")
    links = writer_links(s, {"openrouter": object(), "groq": object()})
    assert [(link.provider, link.model) for link in links].count(("openrouter", "dots-studio/dots-3-note-preview:free")) == 1


def test_build_writer_uses_plain_client_when_no_fallbacks():
    client = object()
    w = build_writer(settings(writer_fallbacks=""), {"groq": client})
    assert w._client is client and w._model == "openai/gpt-oss-120b"
    w2 = build_writer(settings(), {"groq": client, "openrouter": object()})
    assert isinstance(w2._client, FallbackChat) and w2._model.startswith("chain(groq:openai/gpt-oss-120b+")


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"writer_provider": "openrouter"}, "OPENROUTER_API_KEY"),
        ({"writer_provider": "modelscope"}, "MODELSCOPE_API_KEY"),
        ({"writer_provider": "zenmux"}, "ZENMUX_API_KEY"),
        ({"writer_fallbacks": "bogus:model"}, "unknown provider"),
    ],
)
def test_config_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        settings(**kw)


def test_all_provider_keys_are_redacted_secrets():
    s = settings(openrouter_api_key="sk-or-1", modelscope_api_key="ms-1", zenmux_api_key="sk-mg-1")
    for secret in ("sk-or-1", "ms-1", "sk-mg-1", "g"):
        assert secret in s.secret_values()
    assert "sk-or-1" not in repr(s)


def test_second_groq_key_is_optional_and_skipped_without_it():
    from app.services import writer_links

    s = settings()
    links = writer_links(s, {"groq": object(), "openrouter": object()})
    assert ("groq2", "openai/gpt-oss-120b") not in [(link.provider, link.model) for link in links]
    links = writer_links(s, {"groq": object(), "groq2": object(), "openrouter": object()})
    assert [(link.provider, link.model) for link in links][:3] == [
        ("groq", s.groq_chat_model), ("groq2", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b")
    ]


def test_second_groq_key_is_redacted():
    assert "gsk-second" in settings(groq_api_key_2="gsk-second").secret_values()


async def test_failover_browser_uses_the_next_account():
    from app.research import FailoverBrowser

    class Down:
        provider_name = "groq"

        async def browse(self, model, messages, max_tokens=4096):
            raise RuntimeError("429 tokens per day")

    class Up:
        provider_name = "groq2"

        async def browse(self, model, messages, max_tokens=4096):
            return "page"

    assert await FailoverBrowser([Down(), None, Up()]).browse("m", []) == "page"
    with pytest.raises(RuntimeError, match="per day"):
        await FailoverBrowser([Down()]).browse("m", [])


def test_qwen_gets_no_hidden_reasoning():
    from app.groq import reasoning_effort_for

    assert reasoning_effort_for("qwen/qwen3.8-27b", "low") == "none"
    assert reasoning_effort_for("openai/gpt-oss-120b", "low") == "low"
    assert reasoning_effort_for("some/other", "low") is None
