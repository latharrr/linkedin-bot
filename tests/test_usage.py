import json
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.nvidia import NvidiaClient, NvidiaError
from app.research import Researcher
from app.usage import (
    GROQ_TOKENS_PER_DAY,
    UsageRecorder,
    call_row,
    collect,
    groq_card,
    linkedin_card,
    openrouter_card,
    summarize,
    supabase_card,
    tavily_card,
)
from tests.fakes import FakeTavily

_REAL_CONNECT = socket.socket.connect  # captured before the autouse no-network fixture patches them
_REAL_CREATE = socket.create_connection
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


# ── call_row ─────────────────────────────────────────────────────────────────
def test_call_row_success_captures_tokens_and_ratelimit_headers():
    resp = httpx.Response(
        200,
        json={"usage": {"prompt_tokens": 72, "completion_tokens": 10, "total_tokens": 82}},
        headers={"x-ratelimit-remaining-requests": "963", "x-ratelimit-limit-requests": "1000",
                 "x-ratelimit-remaining-tokens": "7927", "x-ratelimit-limit-tokens": "8000"},
    )
    row = call_row("groq", "completions", "openai/gpt-oss-120b", resp)
    assert row == {
        "provider": "groq", "endpoint": "completions", "model": "openai/gpt-oss-120b", "ok": True, "status": 200,
        "prompt_tokens": 72, "completion_tokens": 10, "total_tokens": 82,
        "ratelimit_remaining_requests": 963, "ratelimit_limit_requests": 1000,
        "ratelimit_remaining_tokens": 7927, "ratelimit_limit_tokens": 8000,
    }


def test_call_row_failure_and_exception():
    row = call_row("openrouter", "completions", "m:free", httpx.Response(429, text="rate-limited upstream"))
    assert (row["ok"], row["status"], row["error"]) == (False, 429, "rate-limited upstream")
    row = call_row("nvidia", "embeddings", "m", None, httpx.ReadTimeout("slow"))
    assert row == {"provider": "nvidia", "endpoint": "embeddings", "model": "m", "ok": False, "error": "ReadTimeout"}


# ── every HTTP attempt is recorded; recording never breaks a call ────────────
async def test_api_client_records_each_attempt_including_retries():
    responses = iter([httpx.Response(503), httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 9}})])
    rows = []

    async def no_sleep(_):
        return None

    client = NvidiaClient("k", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(responses))),
                          chat_timeout=1, embed_timeout=1, image_timeout=1, sleep=no_sleep)
    client.recorder = rows.append
    await client.chat("model-x", [])
    assert [(r["provider"], r["status"], r["ok"]) for r in rows] == [("nvidia", 503, False), ("nvidia", 200, True)]
    assert rows[1]["total_tokens"] == 9 and rows[1]["model"] == "model-x"


async def test_flux_row_uses_endpoint_as_model_and_exceptions_recorded():
    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    rows = []
    client = NvidiaClient("k", httpx.AsyncClient(transport=httpx.MockTransport(boom)), chat_timeout=1, embed_timeout=1, image_timeout=1)
    client.recorder = rows.append
    with pytest.raises(NvidiaError):
        await client.flux("p")
    assert rows == [{"provider": "nvidia", "endpoint": "flux.1-dev", "model": "flux.1-dev", "ok": False, "error": "ReadTimeout"}]


async def test_broken_recorder_never_breaks_the_call():
    def bad(row):
        raise RuntimeError("ledger down")

    client = NvidiaClient("k", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))),
                          chat_timeout=1, embed_timeout=1, image_timeout=1)
    client.recorder = bad
    assert (await client.chat("m", [])).text == "ok"


async def test_tavily_records_credits_by_depth():
    rows = []
    r = Researcher(FakeTavily(), 10)
    r.recorder = rows.append
    await r.research("x", 2026)  # advanced depth
    await r.news_topic(["AI"], 0)  # basic depth
    assert [(x["model"], x["credits"], x["ok"]) for x in rows] == [("advanced", 2, True), ("basic", 1, True)]


async def test_usage_recorder_writes_in_background_and_swallows_db_errors():
    class DB:
        def __init__(self):
            self.rows = []

        async def record_api_call(self, row):
            if row.get("fail"):
                raise RuntimeError("insert failed")
            self.rows.append(row)

    db = DB()
    rec = UsageRecorder(db)
    rec({"provider": "groq"})
    rec({"provider": "x", "fail": True})
    await rec.drain()
    assert db.rows == [{"provider": "groq"}]


# ── summaries and cards ──────────────────────────────────────────────────────
ROWS = [  # newest first, as queried
    {"provider": "groq", "model": "openai/gpt-oss-120b", "ok": False, "status": 429, "error": "tokens per day", "at": "t3"},
    {"provider": "groq", "model": "openai/gpt-oss-120b", "ok": True, "status": 200, "total_tokens": 45_000,
     "ratelimit_remaining_requests": 950, "ratelimit_limit_requests": 1000, "ratelimit_remaining_tokens": 2000, "ratelimit_limit_tokens": 8000},
    {"provider": "groq", "model": "openai/gpt-oss-120b", "ok": True, "status": 200, "total_tokens": 5_000,
     "ratelimit_remaining_requests": 999, "ratelimit_limit_requests": 1000},
    {"provider": "tavily", "model": "advanced", "ok": True, "credits": 2},
    {"provider": "modelscope", "model": "deepseek-ai/DeepSeek-V4-Pro", "ok": False, "status": 401, "error": "bind account"},
]


def test_summarize():
    s = summarize(ROWS)
    g = s["groq"]
    assert (g.calls, g.ok, g.errors, g.rate_limited, g.tokens) == (3, 2, 1, 1, 50_000)
    assert g.last_error == "429 tokens per day"
    assert g.latest_headers["ratelimit_remaining_requests"] == 950  # newest row that had headers
    assert s["tavily"].credits == 2
    assert s["modelscope"].ok == 0


def test_groq_card_meters_and_warn_on_rate_limit():
    c = groq_card(True, summarize(ROWS)["groq"])
    labels = {m["label"]: m for m in c["meters"]}
    assert labels["Tokens, rolling 24 h"]["used"] == 50_000 and labels["Tokens, rolling 24 h"]["limit"] == GROQ_TOKENS_PER_DAY
    assert labels["Requests today"]["used"] == 50 and labels["Requests today"]["source"] == "headers"
    assert labels["Tokens this minute"]["used"] == 6000
    assert c["status"] == "warn" and c["last_error"] == "429 tokens per day"


@pytest.mark.parametrize(
    "configured,stats,expected",
    [(False, None, "off"), (True, None, "idle"), (True, summarize(ROWS)["modelscope"], "error")],
)
def test_gateway_statuses(configured, stats, expected):
    from app.usage import gateway_card

    assert gateway_card("modelscope", "ModelScope", configured, stats, "hint")["status"] == expected


def test_openrouter_card_uses_live_free_request_counter():
    key = {"usage": 0, "is_free_tier": True, "free_model_daily_requests": {"used": 46, "limit": 50, "remaining": 4}}
    c = openrouter_card(True, None, key, None)
    assert c["meters"][0] == {"label": "Free-model requests today", "used": 46, "limit": 50, "unit": "req", "source": "live", "note": ""}
    assert c["status"] == "warn"  # 92 % used
    assert openrouter_card(True, None, None, "HTTPStatusError: 401")["status"] == "error"


def test_tavily_supabase_linkedin_cards():
    t = tavily_card(True, None, {"account": {"current_plan": "Researcher", "plan_usage": 10, "plan_limit": 1000}}, None)
    assert t["meters"][0]["used"] == 10 and t["status"] == "ok"
    sb = supabase_card({"db_bytes": 12_000_000, "storage_bytes": 460_000, "storage_objects": 2, "rows": {"posts": 1}}, None)
    assert [m["source"] for m in sb["meters"]] == ["live", "live"] and "2 images" in sb["meters"][1]["note"]
    assert supabase_card(None, "boom")["status"] == "error"
    assert linkedin_card(None, False, NOW)["status"] == "off"
    li = linkedin_card((NOW - timedelta(days=55)).isoformat(), True, NOW)
    assert li["meters"][0]["used"] == 55 and li["status"] == "warn"


# ── collect(): live checks server-side, no secrets out ───────────────────────
class LedgerDB:
    async def api_calls_since(self, since):
        return ROWS

    async def count_api_calls(self, provider):
        return 42

    async def resource_usage(self):
        return {"db_bytes": 1, "storage_bytes": 2, "storage_objects": 0, "rows": {}}

    async def get_setting(self, key):
        return None


def settings(**kw):
    base = dict(
        telegram_bot_token="123:tg-secret", my_chat_id=1, supabase_url="https://x.supabase.co", supabase_service_key="sb-secret",
        nvidia_api_key="nv-secret", groq_api_key="gsk-secret", openrouter_api_key="or-secret", tavily_api_key="tv-secret",
        modelscope_api_key="ms-secret", zenmux_api_key="zm-secret", linkedin_api_version="202601",
    )
    return Settings(_env_file=None, **{**base, **kw})


def live_handler(req: httpx.Request) -> httpx.Response:
    if req.url.host == "openrouter.ai":
        assert req.headers["authorization"] == "Bearer or-secret"
        return httpx.Response(200, json={"data": {"usage": 0, "is_free_tier": True, "free_model_daily_requests": {"used": 32, "limit": 50}}})
    if req.url.host == "api.tavily.com":
        return httpx.Response(200, json={"account": {"current_plan": "Researcher", "plan_usage": 3, "plan_limit": 1000}})
    if req.url.host == "api.telegram.org":
        return httpx.Response(200, json={"ok": True, "result": {"username": "linkedinwala_bot"}})
    return httpx.Response(404)


async def test_collect_builds_every_card_and_leaks_no_secret():
    s = settings()
    data = await collect(s, LedgerDB(), httpx.AsyncClient(transport=httpx.MockTransport(live_handler)), NOW)
    ids = [c["id"] for c in data["cards"]]
    assert ids == ["publishing", "groq", "openrouter", "tavily", "nvidia", "brightdata", "jev", "newsdata", "modelscope", "zenmux", "supabase", "linkedin", "telegram"]
    by = {c["id"]: c for c in data["cards"]}
    assert by["openrouter"]["meters"][0]["used"] == 32
    assert by["nvidia"]["meters"][0]["used"] == 42
    assert by["telegram"]["facts"][0] == ["Bot", "@linkedinwala_bot"]
    assert by["modelscope"]["status"] == "error"
    blob = json.dumps(data)
    for secret in ("tg-secret", "sb-secret", "nv-secret", "gsk-secret", "or-secret", "tv-secret", "ms-secret", "zm-secret"):
        assert secret not in blob


async def test_collect_survives_every_live_check_failing():
    class DeadDB(LedgerDB):
        async def api_calls_since(self, since):
            raise RuntimeError("relation bot_api_calls does not exist")

        async def resource_usage(self):
            raise RuntimeError("rpc missing")

    data = await collect(settings(), DeadDB(), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), NOW)
    by = {c["id"]: c for c in data["cards"]}
    assert "bot_api_calls" in data["ledger_error"]
    assert by["supabase"]["status"] == "error" and by["telegram"]["status"] == "error" and by["openrouter"]["status"] == "error"


# ── the local server ─────────────────────────────────────────────────────────
@pytest.fixture
def dashboard(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _REAL_CONNECT)  # loopback only, for this test
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE)
    sys.path.insert(0, str(ROOT / "scripts"))
    import usage_dashboard

    monkeypatch.setattr(usage_dashboard.CACHE, "get", lambda force=False: {"generated_at": "now", "cards": [], "forced": force})
    server = ThreadingHTTPServer(("127.0.0.1", 0), usage_dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def get(port, path):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Type"), resp.read()


def test_server_routes(dashboard):
    status, ctype, body = get(dashboard, "/")
    assert status == 200 and ctype.startswith("text/html") and b"API &amp; resource usage" in body
    status, ctype, body = get(dashboard, "/api/usage?refresh=1")
    assert status == 200 and json.loads(body)["forced"] is True
    assert get(dashboard, "/nope")[0] == 404


def test_server_defaults_to_loopback():
    sys.path.insert(0, str(ROOT / "scripts"))
    import usage_dashboard

    src = Path(usage_dashboard.__file__).read_text()
    assert 'default="127.0.0.1"' in src


def test_page_escapes_provider_text():
    html = (ROOT / "app" / "usage_page.html").read_text()
    assert "const esc =" in html and "${esc(c.last_error)}" in html  # error bodies from providers are escaped
    assert "http" not in html.split("<script>")[1].split("fetch(")[0]  # no external calls from the page
