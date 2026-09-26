import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.groq import CHAT_URL, GroqClient, GroqError, page_evidence
from app.pipeline import repair_if_needed, run_brief
from app.research import BrowsingResearcher, Researcher, ground_facts, page_for
from app.services import build_researcher, build_writer
from app.writer import Writer
from tests.conftest import CHAT_ID
from tests.fakes import FakeNvidia, FakeTavily, user_text

PAGE_A = "L1: URL: https://www.example.org/report/\nL2: Why 90% of Student Entrepreneurs Drop Out\nL40: 49 % cite sales as the biggest hurdle."
PAGE_B = "L1: Student Startup Monitor 2024\nL9: roughly one-third (33 %) point to raising capital"

# Realistic Groq executed_tools: a search stub (empty content) then two opened pages.
TOOLS = [
    {"index": 0, "type": "browser_search", "name": "browser.search", "arguments": '{"query": "x"}', "output": "…",
     "search_results": {"results": [{"title": "stub", "url": "https://stub.example/only-searched", "content": "", "score": 0}]}},
    {"index": 1, "type": "browser.open", "name": "browser.open", "arguments": '{"id": 2}', "output": PAGE_A,
     "search_results": {"results": [{"title": "example.org", "url": "https://www.example.org/report/", "content": PAGE_A, "score": 0}]}},
    {"index": 2, "type": "browser.open", "name": "browser.open", "arguments": '{"id": 3}', "output": PAGE_B,
     "search_results": {"results": [{"title": "monitor", "url": "https://uni.example/ssm24.pdf", "content": PAGE_B, "score": 0}]}},
]


def facts(*items):
    return json.dumps([{"claim": c, "source": s, "url": u} for c, s, u in items])


# ── Groq client ──────────────────────────────────────────────────────────────
async def test_chat_body_keeps_reasoning_out():
    nv = FakeNvidia()
    await Writer(nv.groq_client(), "openai/gpt-oss-120b", 0.85).draft("a", "b", {"sub_template": "insight_list"}, "R", {"current_role": "x"}, [])
    body = nv.groq_calls[0]
    assert body["model"] == "openai/gpt-oss-120b" and body["temperature"] == 0.85
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False
    assert "tools" not in body
    assert nv.auth_headers[0] == "Bearer gsk-test"
    assert CHAT_URL == "https://api.groq.com/openai/v1/chat/completions"


async def test_browse_requests_browser_search_and_returns_opened_pages_only():
    nv = FakeNvidia()
    nv.browse_text, nv.browse_tools = "[]", TOOLS
    res = await nv.groq_client().browse("openai/gpt-oss-120b", [{"role": "user", "content": "q"}])
    body = nv.groq_calls[0]
    assert body["tools"] == [{"type": "browser_search"}] and body["tool_choice"] == "required"
    assert set(res.pages) == {"https://www.example.org/report/", "https://uni.example/ssm24.pdf"}  # stub skipped
    assert "49 %" in res.pages["https://www.example.org/report/"]


def test_page_evidence_merges_repeat_visits():
    msg = {"executed_tools": [
        {"search_results": {"results": [{"url": "https://a", "content": "part one"}]}},
        {"search_results": {"results": [{"url": "https://a", "content": "part two"}]}},
        {"search_results": None},
    ]}
    assert page_evidence(msg) == {"https://a": "part one\npart two"}
    assert page_evidence({}) == {}


async def test_groq_retries_429_then_errors_cleanly():
    calls = []

    async def no_sleep(_):
        return None

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"}, text="rate limited")

    client = GroqClient("gsk-secret", httpx.AsyncClient(transport=httpx.MockTransport(handler)), chat_timeout=1, browse_timeout=1, sleep=no_sleep)
    with pytest.raises(GroqError) as exc:
        await client.chat("m", [])
    # One retry only: a busier pipeline needs the fallback chain (groq2, then others)
    # to take over quickly instead of this client blocking on its own for minutes.
    assert len(calls) == 2 and exc.value.status == 429 and "gsk-secret" not in str(exc.value)


async def test_groq_calls_are_serialised():
    import asyncio

    active, peak = 0, 0

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            return httpx.Response(200, json=body)

    client = GroqClient("k", httpx.AsyncClient(transport=SlowTransport()), chat_timeout=1, browse_timeout=1)
    await asyncio.gather(*(client.chat("m", []) for _ in range(4)))
    assert peak == 1


# ── grounding: a fact survives only if its numbers are on the page it cites ──
def test_ground_facts_keeps_only_page_backed_numbers():
    pages = {"https://www.example.org/report/": PAGE_A, "https://uni.example/ssm24.pdf": PAGE_B}
    kept = ground_facts(
        [
            {"claim": "90% of student entrepreneurs drop out.", "source": "Mashauri", "url": "https://example.org/report"},  # url drift ok
            {"claim": "49% cite sales as the biggest hurdle.", "source": "Mashauri", "url": "https://www.example.org/report/"},
            {"claim": "72% quit within a year.", "source": "Mashauri", "url": "https://www.example.org/report/"},  # invented
            {"claim": "33% point to raising capital.", "source": "SSM", "url": "https://uni.example/ssm24.pdf"},
            {"claim": "12% pivot twice.", "source": "Blog", "url": "https://stub.example/only-searched"},  # never opened
            {"claim": "Student founders struggle with sales.", "source": "SSM", "url": "https://uni.example/ssm24.pdf"},  # no number
            {"claim": "33% point to raising capital.", "source": "SSM", "url": "https://uni.example/ssm24.pdf"},  # duplicate
            {"claim": "", "url": "https://uni.example/ssm24.pdf"},
        ],
        pages,
    )
    assert [k["content"] for k in kept] == [
        "90% of student entrepreneurs drop out.",
        "49% cite sales as the biggest hurdle.",
        "33% point to raising capital.",
        "Student founders struggle with sales.",
    ]
    assert kept[0] == {"title": "Mashauri", "url": "https://example.org/report", "content": "90% of student entrepreneurs drop out."}


def test_page_for_url_normalisation():
    pages = {"https://www.example.org/report/": "A"}
    assert page_for("http://example.org/report", pages) == "A"
    assert page_for("https://example.org/report/#section", pages) == "A"
    assert page_for("https://example.org/other", pages) is None


# ── BrowsingResearcher ───────────────────────────────────────────────────────
GOOD = facts(
    ("90% of student entrepreneurs drop out.", "Mashauri", "https://www.example.org/report/"),
    ("49% cite sales as the biggest hurdle.", "Mashauri", "https://www.example.org/report/"),
    ("33% point to raising capital.", "SSM", "https://uni.example/ssm24.pdf"),
)


async def test_every_source_runs_and_results_interleave():
    nv, tav = FakeNvidia(), FakeTavily()
    nv.browse_text, nv.browse_tools = "Sure:\n" + GOOD, TOOLS
    res = await BrowsingResearcher(nv.groq_client(), "openai/gpt-oss-120b", Researcher(tav, 10)).research("student founders", 2026)
    assert res["source"] == "browse+tavily" and len(res["results"]) == 8 and len(tav.queries) == 1
    # round-robin: browse, tavily, browse, tavily… so both sources survive the cap
    assert res["results"][0]["content"].startswith("90% of student") and res["results"][1]["url"].startswith("https://example.com/")
    prompt = user_text(nv.groq_calls[0])
    assert "student founders" in prompt and "2026 or 2025" in prompt


async def test_too_few_grounded_facts_topped_up_from_tavily():
    nv, tav = FakeNvidia(), FakeTavily()
    nv.browse_text = facts(("90% drop out.", "M", "https://www.example.org/report/"), ("72% quit.", "M", "https://www.example.org/report/"))
    nv.browse_tools = TOOLS
    res = await BrowsingResearcher(nv.groq_client(), "m", Researcher(tav, 10)).research("x", 2026)
    assert res["source"] == "browse+tavily" and res["results"][0]["content"] == "90% drop out."
    assert len(res["results"]) == 8 and len(tav.queries) == 1


async def test_browse_failure_falls_back_to_tavily():
    nv, tav = FakeNvidia(), FakeTavily()
    nv.browse_status = 400
    res = await BrowsingResearcher(nv.groq_client(), "m", Researcher(tav, 10)).research("x", 2026)
    assert res["source"] == "tavily" and len(res["results"]) == 8


async def test_no_fallback_and_nothing_grounded_returns_empty_not_error():
    nv = FakeNvidia()
    nv.browse_text = "not json at all"
    res = await BrowsingResearcher(nv.groq_client(), "m", None).research("x", 2026)
    assert res["results"] == [] and res["source"] == "none"


async def test_news_topic_prefers_tavily_then_browses():
    nv, tav = FakeNvidia(), FakeTavily()
    assert (await BrowsingResearcher(nv.groq_client(), "m", Researcher(tav, 10)).news_topic(["AI agents"], 0)).startswith("Source 0")
    assert nv.groq_calls == []
    nv.browse_text = '{"title": "Agent startup raises $20M", "summary": "A seed round.", "url": "https://news.example/a"}'
    topic = await BrowsingResearcher(nv.groq_client(), "m", None).news_topic(["AI agents", "edtech"], 1)
    assert topic == "Agent startup raises $20M — A seed round. (niche: edtech; source: https://news.example/a)"
    assert "edtech" in user_text(nv.groq_calls[0])


# ── number repair (step 7b) ──────────────────────────────────────────────────
class ScriptedWriter:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def repair_numbers(self, draft, numbers, research):
        self.calls.append(numbers)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


CORPUS = "47% of founders quit"
DRAFT = "47% of founders quit.\nWe charged ₹999 for 1,200 users.\nLine three stays.\nWhat would you do?"


async def test_repair_skipped_when_nothing_unverified():
    w = ScriptedWriter("x")
    assert await repair_if_needed(w, "47% of founders quit.", "R", CORPUS) == "47% of founders quit." and w.calls == []


async def test_repair_applied_when_it_removes_flags():
    fixed = "47% of founders quit.\nWe charged less for early users.\nLine three stays.\nWhat would you do?"
    w = ScriptedWriter(fixed)
    assert await repair_if_needed(w, DRAFT, "R", CORPUS) == fixed
    assert w.calls == [["999", "1200"]]


@pytest.mark.parametrize(
    "reply",
    [
        "What?",  # gutted the post
        "47% of founders quit.\nWe charged ₹999 for 1,200 users and 5,000 more.\nLine three stays.\nWhat would you do?",  # worse
        RuntimeError("api down"),
    ],
)
async def test_bad_repair_keeps_original(reply):
    assert await repair_if_needed(ScriptedWriter(reply), DRAFT, "R", CORPUS) == DRAFT


async def test_pipeline_repairs_unsupported_numbers_end_to_end(svc, fakes):
    fakes["tavily"].results = [{"title": "t", "url": "https://u", "content": "Founders quit for many reasons.", "score": 1}]
    post = await run_brief(svc, CHAT_ID, "student founders quitting")
    row = fakes["db"].posts[post.id]
    assert "47%" not in row["draft_a"] and row["draft_b"] == ""  # fake drafts invent "47%"; repair removed it
    repair_calls = [c for c in fakes["nvidia"].chat_calls if user_text(c).startswith("Edit this LinkedIn post")]
    assert len(repair_calls) == 1 and "must go: 47%" in user_text(repair_calls[0])  # one post, one repair


async def test_pipeline_repair_can_be_disabled(svc, fakes):
    svc.settings = svc.settings.model_copy(update={"number_repair": False})
    fakes["tavily"].results = [{"title": "t", "url": "https://u", "content": "no numbers", "score": 1}]
    post = await run_brief(svc, CHAT_ID, "student founders quitting")
    assert "47%" in fakes["db"].posts[post.id]["draft_a"]
    assert not any(user_text(c).startswith("Edit this LinkedIn post") for c in fakes["nvidia"].chat_calls)


# ── provider selection & config ──────────────────────────────────────────────
BASE = dict(
    telegram_bot_token="t", my_chat_id=1, supabase_url="https://x.supabase.co", supabase_service_key="s",
    nvidia_api_key="n", linkedin_api_version="202601",
)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **{**BASE, **kw})


def test_defaults_are_groq_writer_and_browsing_research():
    s = settings(groq_api_key="g")
    assert (s.writer_provider, s.research_provider, s.number_repair, s.groq_chat_model) == ("groq", "browse", True, "openai/gpt-oss-120b")
    assert s.tavily_api_key is None  # Tavily is optional now


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({}, "GROQ_API_KEY"),
        ({"writer_provider": "nvidia"}, "GROQ_API_KEY"),  # browse research still needs Groq
        ({"writer_provider": "nvidia", "research_provider": "tavily"}, "TAVILY_API_KEY"),
    ],
)
def test_missing_provider_keys_fail_fast(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        settings(**kw)


def test_nvidia_and_tavily_only_is_valid():
    s = settings(writer_provider="nvidia", research_provider="tavily", tavily_api_key="tv")
    assert s.groq_api_key is None


def test_build_writer_and_researcher_follow_settings():
    nv = FakeNvidia()
    groq, nvidia = nv.groq_client(), nv.client()
    s = settings(groq_api_key="g", tavily_api_key="tv", writer_fallbacks="")
    assert build_writer(s, {"groq": groq, "nvidia": nvidia})._client is groq
    assert build_writer(s.model_copy(update={"writer_provider": "nvidia"}), {"groq": groq, "nvidia": nvidia})._client is nvidia
    r = build_researcher(s, groq)
    assert isinstance(r, BrowsingResearcher) and isinstance(r._fallback, Researcher)
    assert build_researcher(settings(groq_api_key="g"), groq)._fallback is None
    assert isinstance(build_researcher(s.model_copy(update={"research_provider": "tavily"}), groq), Researcher)


def test_secret_values_include_groq_and_skip_missing():
    s = settings(groq_api_key="gsk-live")
    assert "gsk-live" in s.secret_values() and "" not in s.secret_values()
