"""Link reading, the Bright Data client (budget, async snapshots) and fresh-research grounding."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.brightdata import (
    CHATGPT_SEARCH,
    X_POSTS,
    BrightData,
    BudgetExhausted,
    answer_facts,
    clean_url,
    social_dataset,
)
from app.linkread import (
    LinkReader,
    brief_with_titles,
    find_urls,
    html_to_text,
    is_public_url,
)
from app.research import FreshSearch


async def _no_sleep(_):
    return None


ARTICLE = (
    "<html><head><title>Ignored</title><meta property=\"og:title\" content=\"Building a Harness with Jev\"></head>"
    "<body><nav>Menu Home Pricing</nav><script>var x=1;</script><article><h1>Building a Harness</h1>"
    + "<p>Jev returns typed decisions with calibrated probabilities in 40 ms.</p>" * 12
    + "</article><footer>© 2026</footer></body></html>"
)


def test_find_urls_and_titles():
    text = "https://www.langchain.com/blog/building-a-harness-with-jev something on this? also https://x.com/a/status/1."
    assert find_urls(text) == ["https://www.langchain.com/blog/building-a-harness-with-jev", "https://x.com/a/status/1"]
    page = {"title": "Building a Harness with Jev", "url": "u", "content": "c"}
    out = brief_with_titles(text, {"https://www.langchain.com/blog/building-a-harness-with-jev": page})
    assert out.startswith('"Building a Harness with Jev" (https://www.langchain.com/blog/building-a-harness-with-jev)')


@pytest.mark.parametrize(
    ("url", "ok"),
    [("https://example.com/a", True), ("http://localhost:8787/", False), ("http://127.0.0.1/x", False),
     ("http://10.0.0.5/", False), ("file:///etc/passwd", False), ("http://printer.local/", False)],
)
def test_only_public_urls(url, ok):
    assert is_public_url(url) is ok


def test_html_to_text_prefers_article_and_drops_chrome():
    title, text = html_to_text(ARTICLE)
    assert title == "Building a Harness with Jev"
    assert "calibrated probabilities in 40 ms" in text and "Menu Home" not in text and "var x" not in text


async def test_link_reader_fetches_and_truncates():
    def handler(req):
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=ARTICLE)

    reader = LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    pages = await reader.read_all("https://blog.example.com/jev something on this?")
    page = pages["https://blog.example.com/jev"]
    assert page["title"] == "Building a Harness with Jev" and len(page["content"]) <= 4000


@pytest.mark.parametrize(("status", "ctype"), [(403, "text/html"), (200, "application/pdf")])
async def test_link_reader_skips_unusable_pages(status, ctype):
    def handler(req):
        return httpx.Response(status, headers={"content-type": ctype}, text=ARTICLE)

    reader = LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await reader.read("https://blog.example.com/jev") is None


# ── Bright Data client ───────────────────────────────────────────────────────
class LedgerDB:
    def __init__(self, used=0):
        self.rows = [{"provider": "brightdata", "ok": True, "credits": 1}] * used

    async def api_calls_since(self, since):
        return list(self.rows)


class FakeBrightData:
    def __init__(self, mode="sync", records=None):
        self.mode = mode
        self.records = records or [{"answer_text": "ok"}]
        self.calls = []
        self.polls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, dict(req.url.params), req.content))
        if req.url.path == "/datasets/v3/scrape":
            if self.mode == "async":
                return httpx.Response(202, json={"snapshot_id": "sd_1", "message": "still in progress"})
            return httpx.Response(200, json=self.records)
        if req.url.path == "/datasets/v3/progress/sd_1":
            self.polls += 1
            return httpx.Response(200, json={"status": "ready" if self.polls >= 2 else "running"})
        if req.url.path == "/datasets/v3/snapshot/sd_1":
            return httpx.Response(200, json=self.records)
        return httpx.Response(404)


def bd_client(fake, used=0, cap=10):
    bd = BrightData(httpx.AsyncClient(transport=httpx.MockTransport(fake)), "bd-key", LedgerDB(used), cap, sleep=_no_sleep)
    bd.recorder = lambda row: fake.calls.append(("ledger", row))
    return bd


async def test_sync_scrape_records_one_credit():
    fake = FakeBrightData()
    rec = await bd_client(fake).chatgpt_search("latest agent stats")
    assert rec == {"answer_text": "ok"}
    method, path, params, body = fake.calls[0]
    assert params["dataset_id"] == CHATGPT_SEARCH and json.loads(body)["input"][0]["web_search"] is True
    ledger = [c[1] for c in fake.calls if c[0] == "ledger"]
    assert ledger[0]["provider"] == "brightdata" and ledger[0]["credits"] == 1 and ledger[0]["endpoint"] == "chatgpt_search"


async def test_slow_scrape_polls_the_snapshot():
    fake = FakeBrightData(mode="async")
    assert await bd_client(fake).chatgpt_search("x") == {"answer_text": "ok"}
    assert fake.polls == 2


async def test_daily_cap_blocks_before_any_request():
    fake = FakeBrightData()
    with pytest.raises(BudgetExhausted):
        await bd_client(fake, used=10, cap=10).chatgpt_search("x")
    assert fake.calls == []


def test_social_dataset_routing():
    assert social_dataset("https://x.com/someone/status/123") == X_POSTS
    assert social_dataset("https://x.com/someone") is None
    assert social_dataset("https://www.linkedin.com/posts/deepanshulathar_abc") is not None
    assert social_dataset("https://www.linkedin.com/in/deepanshulathar/") is None
    assert social_dataset("https://example.com/status/1") is None


async def test_x_link_read_through_brightdata():
    fake = FakeBrightData(records=[{"description": "Shipping agents without approval gates is a bet.", "user_posted": "someone"}])
    reader = LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), brightdata=bd_client(fake))
    page = await reader.read("https://x.com/someone/status/123")
    assert page["content"].startswith("Shipping agents") and page["title"] == "Post by someone"


# ── ChatGPT answer → grounded facts ─────────────────────────────────────────
RECORD = {
    "answer_text_markdown": (
        "Here are 3 specific 2026 facts:\n\n"
        "1.  **61% of organizations have AI approval processes.** KPMG's Q2 2026 survey found 61% have approval processes. \\[1\\]\n"
        "2.  **38% require human approval** when an agent exceeds scope, per CSA 2026. \\[2\\]\n"
        "3.  **97% of teams love agents** according to a vibe check. \\[3\\]\n"
    ),
    "links_attached": [
        {"url": "https://kpmg.com/pulse?utm_source=chatgpt.com", "text": "KPMG Q2 2026 Pulse", "position": 1},
        {"url": "https://csa.org/2026-survey?utm_source=chatgpt.com", "text": "CSA survey", "position": 2},
        {"url": "https://blocked.example.com/vibes?utm_source=chatgpt.com", "text": "Vibes", "position": 3},
    ],
}


def test_clean_url_drops_utm_only():
    assert clean_url("https://a.com/x?utm_source=chatgpt.com&id=3") == "https://a.com/x?id=3"


def test_answer_facts_maps_markers_to_urls():
    facts, cited = answer_facts(RECORD)
    assert [f["urls"] for f in facts] == [["https://kpmg.com/pulse"], ["https://csa.org/2026-survey"], ["https://blocked.example.com/vibes"]]
    assert "\\[" not in facts[0]["claim"] and "**" not in facts[0]["claim"]
    assert cited["https://kpmg.com/pulse"] == "KPMG Q2 2026 Pulse"


async def test_fresh_search_keeps_only_facts_grounded_on_cited_pages():
    class Reader:
        async def read(self, url, max_chars=None):
            pages = {
                "https://kpmg.com/pulse": "In Q2 2026, 61% of organizations reported approval processes for AI.",
                "https://csa.org/2026-survey": "Survey (2026): 24% log it, 11% block it.",  # 38 is missing
            }
            return {"title": "t", "url": url, "content": pages[url]} if url in pages else None

    class BD:
        async def chatgpt_search(self, prompt):
            assert "RAG in Indian startups" in prompt
            return RECORD

    results = await FreshSearch(BD(), Reader()).research("RAG in Indian startups", 2026)
    assert [r["url"] for r in results] == ["https://kpmg.com/pulse"]  # 38% not on page; blocked page dropped
    assert results[0]["title"] == "KPMG Q2 2026 Pulse"


async def test_fresh_search_failure_never_breaks_research(svc, fakes):
    from app.research import BrowsingResearcher

    class Broken:
        async def research(self, brief, year):
            raise BudgetExhausted("cap")

    fakes["nvidia"].browse_text = "[]"
    r = BrowsingResearcher(fakes["nvidia"].groq_client(), "openai/gpt-oss-120b", fallback=svc.researcher, fresh=Broken())
    out = await r.research("student founders", datetime.now(UTC).year)
    assert out["source"] == "tavily" and len(out["results"]) == 8


def test_tavily_snippets_are_tidied_and_off_topic_results_dropped():
    from app.research import _clean_results, tidy_snippet

    raw = "Skip to main content\n# How Do Human Approval Gates Work?\nSign In\nApproval gates pause an agent before any irreversible action is taken by the system.\n[Book a Demo](https://x)"
    assert tidy_snippet(raw) == "Approval gates pause an agent before any irreversible action is taken by the system."  # chrome + short heading gone
    results = [{"url": f"https://s/{i}", "title": f"t{i}", "content": "A relevant sentence that is long enough to keep here.", "score": s}
               for i, s in enumerate([0.9, 0.8, 0.7, 0.35, 0.2])]
    kept = _clean_results({"results": results}, 8)
    assert [r["url"] for r in kept] == ["https://s/0", "https://s/1", "https://s/2"]  # low scores dropped
    few = _clean_results({"results": results[3:]}, 8)
    assert len(few) == 2  # but a thin result set is kept whole


def test_general_topics_get_an_informed_take_not_an_invented_story():
    from app.writer import ROLE_A_TAKE, role_for

    assert role_for("a", "story_cold_open", own_story=False) == ROLE_A_TAKE
    assert role_for("a", "story_cold_open", own_story=True).startswith("story / founder-POV")
    assert "never invent an experience" in ROLE_A_TAKE


async def test_non_html_is_refused_from_headers_without_reading_the_body():
    read = {"n": 0}

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read["n"] += 1
            yield b"%PDF" * 10

    def handler(req):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=Body())

    reader = LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await reader.read("https://slow.example.com/report.pdf") is None
    assert read["n"] == 0


async def test_a_trickling_page_is_cut_off_by_the_total_deadline(monkeypatch):
    import asyncio

    from app import linkread

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(1000):
                await asyncio.sleep(0.01)
                yield b"<p>x</p>"

    def handler(req):
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Trickle())

    monkeypatch.setattr(linkread, "TOTAL_SECONDS", 0.05)
    reader = LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await reader.read("https://slow.example.com/page") is None


async def test_oversized_bodies_stop_at_the_cap(monkeypatch):
    from app import linkread

    def handler(req):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>" + b"a" * 5000 + b"</p>")

    monkeypatch.setattr(linkread, "MAX_BYTES", 1000)
    reader = LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await reader.read("https://big.example.com/page") is None
