"""NewsData.io: fresh headlines for news topics (the 2 PM fallback and "you pick").

Free plan: 200 credits/day, 60 requests/min, up to 10 articles per call, news
delayed ~12 h. One call per topic pick. The key goes in the X-ACCESS-KEY header,
never the URL, so it can't end up in a logged URL.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.log import get_logger
from app.usage import call_row

log = get_logger(__name__)

URL = "https://newsdata.io/api/1/latest"
TIMEOUT = 20.0
CATEGORIES = "technology,business"
NEWS_CONTEXT = 2  # recent articles added to each run's research
MAX_WITH_NEWS = 8  # research results in total (matches research.MAX_RESULTS)
_STOP = set(["a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could", "did", "do", "does", "for", "from", "had", "has", "have", "he", "her", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just", "like", "made", "make", "me", "more", "most", "my", "new", "no", "not", "of", "on", "or", "our", "out", "over", "so", "some", "such", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "those", "to", "up", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your", "about", "after", "again", "all", "also", "any", "because", "before", "being", "both", "each", "few", "own", "same", "should", "too", "very", "via", "word", "words", "post", "draft"])


def key_terms(brief: str) -> list[str]:
    """Distinctive terms for a news query: proper nouns first, then the longest words."""
    import re as _re

    text = _re.sub(r"https?://\S+", " ", brief)
    words = _re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", text)
    uniq = list(dict.fromkeys(w.strip(".") for w in words if w.lower().strip(".") not in _STOP))
    proper = [w for w in uniq if w[0].isupper() and not w.isupper() or (w.isupper() and len(w) <= 5)]
    rest = sorted((w for w in uniq if w not in proper), key=len, reverse=True)
    return (proper + rest)[:4]
# Press-release wires are ads, not news; market-size reports are SEO filler. Never a post topic.
WIRES = ("globenewswire", "prnewswire", "businesswire", "cision", "accesswire", "einpresswire", "newsfilecorp")
_JUNK_TITLE = re.compile(r"market (?:size|share)|forecast,? 20\d\d|press release", re.IGNORECASE)


def _is_wire(a: dict[str, Any]) -> bool:
    where = f"{a.get('source_id') or ''} {a.get('source_url') or ''} {a.get('link') or ''}".lower()
    return any(w in where for w in WIRES)


def pick_article(articles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most authoritative (lowest source_priority) real article with a description."""
    usable = [
        a for a in articles
        if a.get("title") and a.get("description") and a.get("link")
        and not a.get("duplicate") and not _is_wire(a) and not _JUNK_TITLE.search(str(a["title"]))
    ]
    return min(usable, key=lambda a: int(a.get("source_priority") or 10**9), default=None)


class NewsData:
    def __init__(self, http: httpx.AsyncClient, api_key: str, timeout: float = TIMEOUT) -> None:
        self._http = http
        self._headers = {"X-ACCESS-KEY": api_key}
        self._timeout = timeout
        self.recorder: Any = None

    async def latest(self, query: str, field: str = "q", exact: bool = True) -> list[dict[str, Any]]:
        """field: "qInTitle" (headline must contain it) or "q" (anywhere). exact: phrase match."""
        phrase = '"' + query.replace('"', "")[:96] + '"' if exact else query[:100]
        params = {field: phrase, "language": "en", "size": 10, "removeduplicate": 1, "category": CATEGORIES}
        resp: httpx.Response | None = None
        exc: BaseException | None = None
        try:
            resp = await self._http.get(URL, params=params, headers=self._headers, timeout=self._timeout)
            data = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as e:
            exc, data = e, {}
            log.warning("newsdata_failed", extra={"error": type(e).__name__})
        finally:
            if self.recorder is not None:
                try:
                    self.recorder(call_row("newsdata", "latest", None, resp, exc, credits=1))
                except Exception:
                    log.warning("usage_record_skipped", extra={"endpoint": "newsdata"})
        if resp is not None and resp.status_code != 200:
            log.warning("newsdata_failed", extra={"status": resp.status_code})
        return list(data.get("results") or []) if data.get("status") == "success" else []

    async def context(self, brief: str) -> list[dict[str, str]]:
        """Up to NEWS_CONTEXT recent articles on the brief's key terms, as research results."""
        terms = key_terms(brief)
        if not terms:
            return []
        try:
            articles = await self.latest(" AND ".join(terms[:2]), "q", exact=False)
        except Exception:
            return []
        picked = []
        wanted = [t.lower() for t in terms]
        for a in sorted(articles, key=lambda a: int(a.get("source_priority") or 10**9)):
            about = f"{a.get('title') or ''} {a.get('description') or ''}".lower()
            relevant = sum(t in about for t in wanted) >= min(2, len(wanted))  # matched in headline/summary, not buried
            if relevant and a.get("title") and a.get("description") and a.get("link") and not _is_wire(a) and not _JUNK_TITLE.search(str(a["title"])):
                desc = " ".join(str(a["description"]).split())[:400]
                picked.append({"title": f"{a['title']} ({a.get('source_name') or a.get('source_id')}, {str(a.get('pubDate') or '')[:10]})", "url": a["link"], "content": desc})
            if len(picked) >= NEWS_CONTEXT:
                break
        log.info("news_context", extra={"terms": terms[:2], "articles": len(articles), "kept": len(picked)})
        return picked

    async def topic(self, keyword: str) -> str | None:
        """Headline match first (relevant); body match only if no headline has the phrase."""
        article = pick_article(await self.latest(keyword, "qInTitle")) or pick_article(await self.latest(keyword, "q"))
        if article is None:
            return None
        desc = " ".join(str(article["description"]).split())
        if len(desc) > 300:
            desc = desc[:300].rsplit(" ", 1)[0] + "…"
        return f"{article['title']} — {desc} (niche: {keyword}; source: {article['link']})"


class NewsFirst:
    """Wraps a research source: news topics come from NewsData first, then the
    wrapped source's own news_topic (Tavily / browsing). Research is unchanged."""

    def __init__(self, inner: Any, news: NewsData) -> None:
        self._inner = inner
        self._news = news

    async def research(self, brief: str, year: int) -> dict[str, Any]:
        """Research from the wrapped sources plus recent news on the brief's key terms."""
        research, news = await asyncio.gather(self._inner.research(brief, year), self._news.context(brief))
        if news:
            known = {r.get("url") for r in research.get("results") or []}
            extra = [n for n in news if n["url"] not in known][:NEWS_CONTEXT]
            research = {**research, "results": [*research["results"][: MAX_WITH_NEWS - len(extra)], *extra]}
            research["source"] = f"{research.get('source', '')}+newsdata".lstrip("+")
        return research

    async def news_topic(self, keywords: list[str], day_index: int) -> str | None:
        if keywords:
            keyword = keywords[day_index % len(keywords)]
            topic = await self._news.topic(keyword)
            if topic:
                return topic
        return await self._inner.news_topic(keywords, day_index)
