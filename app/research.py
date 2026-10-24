"""Research (SPEC §5.1 step 2) and the 2 PM fallback news topic (§5.4).

Two sources, same output shape ({"query", "results": [{title, url, content}]}):
- BrowsingResearcher: a browsing model (Groq gpt-oss + browser_search). Each fact
  is kept only if its numbers appear in the raw text of the page it cites, so the
  draft number check (app/verify.py) still rests on real source text.
- Researcher: Tavily search snippets. Used directly, or as the browsing fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.log import get_logger
from app.verify import unverified_numbers

log = get_logger(__name__)

MIN_RESULTS, MAX_RESULTS = 5, 8
SNIPPET_CHARS = 1200


class SearchClient(Protocol):
    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]: ...


class ResearchError(RuntimeError):
    pass


MIN_SCORE = 0.4  # Tavily relevance; below this results were off-topic in live runs
KEEP_AT_LEAST = 3
_JUNK_LINE = re.compile(
    r"skip to (?:main )?content|sign in|log ?in|subscribe|cookie|press enter|click to view|book a demo|"
    r"all posts|read here|menu|share this|image \d|min read|^\W*$",
    re.IGNORECASE,
)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def tidy_snippet(text: str) -> str:
    """Page text → readable prose: drop navigation, headings markup, links and short
    chrome lines (Tavily's advanced depth returns raw page chunks)."""
    lines = []
    for line in _MD_LINK.sub(r"\1", text).splitlines():
        line = line.strip().lstrip("#>*|- ").strip()
        if len(line) >= 40 and not _JUNK_LINE.search(line):
            lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _clean_results(raw: dict[str, Any], limit: int) -> list[dict[str, str]]:
    ranked = sorted(raw.get("results") or [], key=lambda r: r.get("score") or 0, reverse=True)
    results = []
    for rank, r in enumerate(ranked):
        if not r.get("url") or not (r.get("content") or r.get("title")):
            continue
        if (r.get("score") or 0) < MIN_SCORE and rank >= KEEP_AT_LEAST:
            continue
        content = tidy_snippet(r.get("content") or "") or (r.get("content") or "").strip()
        results.append({"title": (r.get("title") or "").strip(), "url": r["url"], "content": content[:SNIPPET_CHARS]})
    return results[:limit]


def format_research(research: dict[str, Any] | None) -> str:
    """Numbered, source-attributed facts for prompts."""
    results = (research or {}).get("results") or []
    if not results:
        return "(no research results)"
    return "\n".join(f"[{i}] {r['title']} — {r['content']} (source: {r['url']})" for i, r in enumerate(results, 1))


def research_corpus(research: dict[str, Any] | None, brief: str = "") -> str:
    """Everything a draft's numbers may legitimately come from (for verify.py)."""
    parts = [brief]
    for r in (research or {}).get("results") or []:
        parts.extend([r.get("title", ""), r.get("content", "")])
    parts.extend(str(f) for f in (research or {}).get("background") or [])  # sourced facts about the author
    return "\n".join(parts)


class Researcher:
    GRACE = 5.0  # outer deadline beyond Tavily's own timeout
    recorder: Any = None  # usage ledger hook (app/usage.py)

    def __init__(self, client: SearchClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def _search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        depth = kwargs.get("search_depth") or "basic"
        try:
            result = await asyncio.wait_for(
                self._client.search(query, timeout=self._timeout, **kwargs), timeout=self._timeout + self.GRACE
            )
        except Exception as exc:
            self._record(depth, ok=False, error=type(exc).__name__)
            if isinstance(exc, TimeoutError):
                raise ResearchError(f"Tavily timed out after {self._timeout}s") from exc
            raise
        self._record(depth, ok=True)
        return result

    def _record(self, depth: str, ok: bool, error: str | None = None) -> None:
        if self.recorder is None:
            return
        row = {"provider": "tavily", "endpoint": "search", "model": depth, "ok": ok, "credits": 2 if depth == "advanced" else 1}
        if error:
            row["error"] = error
        try:
            self.recorder(row)
        except Exception:
            log.warning("usage_record_skipped", extra={"endpoint": "search"})

    async def research(self, brief: str, year: int) -> dict[str, Any]:
        """SPEC: search `brief + " statistics " + current_year`, keep top 5–8 with URLs."""
        query = f"{brief} statistics {year}"[:400]
        raw = await self._search(query, search_depth="advanced", max_results=MAX_RESULTS)
        results = _clean_results(raw, MAX_RESULTS)
        if len(results) < MIN_RESULTS:
            log.warning("research_thin", extra={"results": len(results)})
        return {"query": query, "results": results}

    async def news_topic(self, keywords: list[str], day_index: int) -> str | None:
        """Pick one fresh news topic. Keywords rotate by IST day so niches take turns."""
        if not keywords:
            return None
        keyword = keywords[day_index % len(keywords)]
        raw = await self._search(f"{keyword} latest news", topic="news", time_range="week", max_results=5)
        results = _clean_results(raw, 1)
        if not results:
            return None
        top = results[0]
        snippet = top["content"][:300].rsplit(" ", 1)[0]
        return f"{top['title']} — {snippet} (niche: {keyword}; source: {top['url']})"


# ── browsing research ────────────────────────────────────────────────────────
MIN_GROUNDED = 3  # fewer grounded facts than this → top up from the fallback


class ResearchSource(Protocol):
    async def research(self, brief: str, year: int) -> dict[str, Any]: ...

    async def news_topic(self, keywords: list[str], day_index: int) -> str | None: ...


class Browser(Protocol):
    async def browse(self, model: str, messages: list[dict[str, str]], *, max_tokens: int = 4096) -> Any: ...


def _norm_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def _json_payload(text: str, opener: str, closer: str) -> Any:
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end <= start:
        raise ValueError("no JSON found")
    return json.loads(text[start : end + 1])


def page_for(url: str, pages: dict[str, str]) -> str | None:
    """Raw text of the opened page a fact cites (tolerates scheme/www/trailing-slash drift)."""
    target = _norm_url(url)
    by_norm = {_norm_url(u): text for u, text in pages.items()}
    if target in by_norm:
        return by_norm[target]
    for norm, text in by_norm.items():
        if target and (norm.startswith(target) or target.startswith(norm)):
            return text
    return None


def ground_facts(facts: list[dict[str, Any]], pages: dict[str, str], limit: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Keep a fact only if it cites a page the model opened AND every number in
    the claim appears in that page's raw text."""
    kept: list[dict[str, str]] = []
    seen: set[str] = set()
    for fact in facts:
        claim = str(fact.get("claim") or "").strip()
        url = str(fact.get("url") or "").strip()
        page = page_for(url, pages) if url else None
        if not claim or page is None or claim in seen:
            continue
        if unverified_numbers(claim, page):
            log.info("research_fact_dropped", extra={"url": url, "reason": "number not on page"})
            continue
        seen.add(claim)
        title = str(fact.get("source") or urlsplit(url).netloc or url).strip()
        kept.append({"title": title, "url": url, "content": claim[:SNIPPET_CHARS]})
    return kept[:limit]


def _interleave(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    """One from each source in turn, skipping repeats of the same fact text. Several facts
    may come from one page, so URLs aren't deduplicated."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if i < len(group):
                key = group[i]["content"][:80].lower()
                if key not in seen:
                    seen.add(key)
                    out.append(group[i])
    return out


def _merge(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for r in group:
            key = r["content"][:80].lower()
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out


class FreshSearch:
    """Bright Data ChatGPT Search (web search on) → facts, each kept only if every
    number in it appears on a page it cites. Cited pages are fetched directly (free);
    a page that blocks the fetch takes its facts with it."""

    MAX_PAGES = 6

    def __init__(self, brightdata: Any, reader: Any) -> None:
        self._bd = brightdata
        self._reader = reader

    async def research(self, brief: str, year: int) -> list[dict[str, str]]:
        from app.brightdata import answer_facts
        from app.writer import load_prompt, render

        record = await self._bd.chatgpt_search(render(load_prompt("fresh_search"), brief=brief, year=year, prev_year=year - 1))
        facts, cited = answer_facts(record)
        wanted = list(dict.fromkeys(u for f in facts for u in f["urls"]))[: self.MAX_PAGES]
        fetched = await asyncio.gather(*(self._reader.read(u, max_chars=None) for u in wanted), return_exceptions=True)
        pages = {u: p["content"] for u, p in zip(wanted, fetched, strict=True) if isinstance(p, dict)}
        results = []
        for fact in facts:
            for url in fact["urls"] or list(pages):
                page = pages.get(url)
                if page and not unverified_numbers(fact["claim"], page):
                    title = cited.get(url) or urlsplit(url).netloc
                    results.append({"title": title, "url": url, "content": fact["claim"][:SNIPPET_CHARS]})
                    break
        log.info("fresh_research", extra={"facts": len(facts), "grounded": len(results), "pages": f"{len(pages)}/{len(wanted)}"})
        return results


class FailoverBrowser:
    """Browsing across Groq accounts: the next one takes over when one fails (daily quota,
    outage). Each client keeps its own rate-limit gate and ledger name."""

    def __init__(self, browsers: list[Any]) -> None:
        self._browsers = [b for b in browsers if b is not None]

    async def browse(self, model: str, messages: list[dict[str, str]], *, max_tokens: int = 4096) -> Any:
        last: Exception | None = None
        for browser in self._browsers:
            try:
                return await browser.browse(model, messages, max_tokens=max_tokens)
            except Exception as exc:
                log.warning("browse_failover", extra={"from": getattr(browser, "provider_name", "?"), "error": f"{type(exc).__name__}: {exc}"[:160]})
                last = exc
        raise last or RuntimeError("no browser configured")


class BrowsingResearcher:
    def __init__(self, browser: Browser, model: str, fallback: Researcher | None = None, fresh: Any = None) -> None:
        self._browser = browser
        self._model = model
        self._fallback = fallback
        self._fresh = fresh  # FreshSearch (Bright Data ChatGPT Search), optional

    async def _browse_json(self, prompt: str, opener: str, closer: str) -> tuple[Any, dict[str, str]]:
        res = await self._browser.browse(self._model, [{"role": "user", "content": prompt}])
        return _json_payload(res.text, opener, closer), res.pages

    async def research(self, brief: str, year: int) -> dict[str, Any]:
        from app.writer import load_prompt, render

        prompt = render(load_prompt("research"), brief=brief, year=year, prev_year=year - 1)
        # Every source at once: Groq browsing, Bright Data ChatGPT Search, Tavily.
        browsed, fresh, searched = await asyncio.gather(
            self._browse_facts(prompt), self._fresh_facts(brief, year), self._search_facts(brief, year)
        )
        # Round-robin, freshest first, so the capped list keeps every source's best facts.
        results = _interleave(fresh, browsed, searched)
        named = (("brightdata", fresh), ("browse", browsed), ("tavily", searched))
        source = "+".join(name for name, got in named if got) or "none"
        if not results:
            log.warning("research_empty", extra={"brief": brief[:80]})
        return {"query": f"browse: {brief}"[:400], "results": results[:MAX_RESULTS], "source": source}

    async def _search_facts(self, brief: str, year: int) -> list[dict[str, str]]:
        if self._fallback is None:
            return []
        try:
            return list((await self._fallback.research(brief, year))["results"])
        except Exception as exc:
            log.warning("tavily_research_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
            return []

    async def _browse_facts(self, prompt: str) -> list[dict[str, str]]:
        try:
            facts, pages = await self._browse_json(prompt, "[", "]")
        except Exception as exc:  # browsing is best-effort; the fallback covers it
            log.warning("browse_research_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
            return []
        facts = facts if isinstance(facts, list) else []
        results = ground_facts([f for f in facts if isinstance(f, dict)], pages)
        log.info("browse_research", extra={"facts": len(facts), "grounded": len(results), "pages": len(pages)})
        return results

    async def _fresh_facts(self, brief: str, year: int) -> list[dict[str, str]]:
        if self._fresh is None:
            return []
        try:
            return await self._fresh.research(brief, year)
        except Exception as exc:  # budget reached, timeout, outage: the other sources carry on
            log.warning("fresh_research_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
            return []

    async def news_topic(self, keywords: list[str], day_index: int) -> str | None:
        if self._fallback is not None:
            return await self._fallback.news_topic(keywords, day_index)
        if not keywords:
            return None
        from app.writer import load_prompt, render

        keyword = keywords[day_index % len(keywords)]
        try:
            story, _pages = await self._browse_json(render(load_prompt("news_topic"), keyword=keyword), "{", "}")
        except Exception as exc:
            log.warning("browse_news_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
            return None
        if not isinstance(story, dict) or not story.get("title"):
            return None
        return f"{story['title']} — {story.get('summary', '')} (niche: {keyword}; source: {story.get('url', '')})"
