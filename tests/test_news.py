"""NewsData.io news topics: article choice, header auth, and fallback to the wrapped source."""

import httpx

from app.news import NewsData, NewsFirst, pick_article

ARTICLES = [
    {"title": "GSC launches A2A hub", "description": "press release", "link": "https://pr/1", "source_id": "globenewswire", "source_priority": 7},
    {"title": "Why Everyone Is Talking About Jev", "description": "The AI that doesn't chat.", "link": "https://forbes/jev", "source_id": "forbes", "source_priority": 154},
    {"title": "Intuitive enzyme design", "description": None, "link": "https://g/1", "source_id": "google", "source_priority": 14},
    {"title": "Opus 5.5 is 40% cheaper", "description": "zdnet on pricing", "link": "https://zdnet/1", "source_id": "zdnet", "source_priority": 1238},
    {"title": "Dup", "description": "d", "link": "https://x/1", "source_id": "x", "source_priority": 1, "duplicate": True},
]


def test_pick_article_skips_wire_variants_and_market_reports():
    arts = [
        {"title": "Exec joins board", "description": "d", "link": "https://www.globenewswire.com/fr/news-release/1", "source_id": "globenewswire_fr", "source_priority": 1},
        {"title": "Coworking Spaces Market Size, Share, Growth, Forecast, 2034", "description": "d", "link": "https://s/1", "source_id": "sr", "source_priority": 2},
        {"title": "Real story", "description": "d", "link": "https://r/1", "source_id": "r", "source_priority": 3},
    ]
    assert pick_article(arts)["link"] == "https://r/1"


def test_pick_article_prefers_authoritative_real_news():
    assert pick_article(ARTICLES)["link"] == "https://forbes/jev"  # wires, no-description and duplicates skipped
    assert pick_article([]) is None


async def test_topic_uses_header_auth_and_records_a_credit():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"status": "success", "results": ARTICLES})

    rows = []
    news = NewsData(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "pub_test")
    news.recorder = rows.append
    topic = await news.topic("AI agents")
    assert topic.startswith("Why Everyone Is Talking About Jev — The AI that doesn't chat.")
    assert "(niche: AI agents; source: https://forbes/jev)" in topic
    assert seen[0].headers["X-ACCESS-KEY"] == "pub_test" and "apikey" not in str(seen[0].url)
    assert seen[0].url.params["qInTitle"] == '"AI agents"' and len(seen) == 1  # headline match found one
    assert rows[0]["provider"] == "newsdata" and rows[0]["credits"] == 1


async def test_news_first_falls_back_when_newsdata_has_nothing():
    class Inner:
        async def news_topic(self, keywords, day_index):
            return "from tavily"

        async def research(self, brief, year):
            return {"results": ["r"]}

    def handler(req):
        return httpx.Response(429, json={"status": "error"})

    wrapped = NewsFirst(Inner(), NewsData(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "k"))
    assert await wrapped.news_topic(["AI"], 0) == "from tavily"
    assert await wrapped.research("b", 2026) == {"results": ["r"]}


async def test_falls_back_to_body_match_when_no_headline_has_the_phrase():
    fields = []

    def handler(req):
        fields.append("qInTitle" if "qInTitle" in req.url.params else "q")
        results = [] if fields[-1] == "qInTitle" else ARTICLES
        return httpx.Response(200, json={"status": "success", "results": results})

    news = NewsData(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "k")
    assert (await news.topic("startup growth")).startswith("Why Everyone Is Talking About Jev")
    assert fields == ["qInTitle", "q"]
