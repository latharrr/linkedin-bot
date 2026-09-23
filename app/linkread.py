"""Links in a brief ("<url> something on this?"): fetch the page and use it as the
primary research source, so the drafts are about what the article actually says
and its numbers count as verified.

Fetching is best-effort: a page that times out, is not HTML/text, or is on a
private address is skipped and the brief runs on search research alone.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.brightdata import social_dataset
from app.log import get_logger

log = get_logger(__name__)

MAX_LINKS = 2
LINK_CHARS = 4000  # ~1k tokens of article per link: enough substance, fits Groq's per-minute cap
MAX_BYTES = 2_000_000
TOTAL_SECONDS = 20.0  # whole fetch, however slowly the bytes arrive
USER_AGENT = "Mozilla/5.0 (compatible; linkedin-draft-bot/1.0; +personal use)"

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|aside|form|iframe|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_MAIN = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_OG_TITLE = re.compile(r"<meta\b[^>]*property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)", re.IGNORECASE)
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|section|br|tr|blockquote)\s*>|<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def find_urls(text: str) -> list[str]:
    seen: list[str] = []
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if url not in seen:
            seen.append(url)
    return seen[:MAX_LINKS]


def is_public_url(url: str) -> bool:
    """http(s) only, and never localhost or a private/link-local IP literal."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def html_to_text(raw: str) -> tuple[str, str]:
    """→ (title, readable text). Prefers <article>/<main>; drops scripts, nav, footers."""
    m = _OG_TITLE.search(raw) or _TITLE.search(raw)
    title = html.unescape(_TAG.sub("", m.group(1))).strip() if m else ""
    body = _DROP_BLOCKS.sub(" ", raw)
    main = _MAIN.search(body)
    if main and len(_TAG.sub("", main.group(2))) > 500:
        body = main.group(2)
    text = html.unescape(_TAG.sub(" ", _BLOCK_END.sub("\n", body)))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\s+", " ", title), "\n".join(ln for ln in lines if len(ln) > 1)


class LinkReader:
    def __init__(self, http: httpx.AsyncClient, timeout: float = 15.0, brightdata: Any = None) -> None:
        self._http = http
        self._timeout = timeout
        self._bd = brightdata  # X / LinkedIn posts can't be read with a plain fetch

    async def read(self, url: str, max_chars: int | None = LINK_CHARS) -> dict[str, str] | None:
        if not is_public_url(url):
            log.warning("link_skipped", extra={"reason": "not a public http(s) url"})
            return None
        if self._bd is not None and social_dataset(url):
            try:
                post = await self._bd.social_post(url)
            except Exception as exc:
                log.warning("link_social_failed", extra={"url": url[:200], "error": f"{type(exc).__name__}: {exc}"[:200]})
                return None
            if post:
                log.info("link_read", extra={"url": url[:200], "chars": len(post["content"]), "via": "brightdata"})
                return {**post, "content": post["content"][:max_chars]}
            return None
        try:
            fetched = await asyncio.wait_for(self._fetch(url), timeout=TOTAL_SECONDS)
        except (httpx.HTTPError, TimeoutError) as exc:
            log.warning("link_fetch_failed", extra={"url": url[:200], "error": type(exc).__name__})
            return None
        if fetched is None:
            return None
        final_url, kind, body = fetched
        title, text = html_to_text(body) if "html" in kind else ("", body)
        if len(text) < 200:
            log.warning("link_too_thin", extra={"url": url[:200], "chars": len(text)})
            return None
        log.info("link_read", extra={"url": url[:200], "chars": len(text)})
        return {"title": title or url, "url": final_url, "content": text[:max_chars]}

    async def _fetch(self, url: str) -> tuple[str, str, str] | None:
        """Headers first: anything that isn't HTML/text, isn't 200, or redirects somewhere
        private is refused before its body is read; bodies stop at MAX_BYTES. (Seen live:
        a slow multi-MB PDF trickled in for 11 minutes, because a per-read timeout never
        fires while bytes keep arriving.)"""
        headers = {"User-Agent": USER_AGENT}
        async with self._http.stream("GET", url, timeout=self._timeout, follow_redirects=True, headers=headers) as resp:
            kind = resp.headers.get("content-type", "").lower()
            declared = int(resp.headers.get("content-length") or 0)
            if resp.status_code != 200 or not ("html" in kind or "text/plain" in kind) or declared > MAX_BYTES:
                log.warning("link_unusable", extra={"url": url[:200], "status": resp.status_code, "type": kind[:60]})
                return None
            if not is_public_url(str(resp.url)):  # a redirect must not land on a private address either
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    log.warning("link_too_big", extra={"url": url[:200]})
                    return None
            body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
            return str(resp.url), kind, body

    async def read_all(self, brief: str) -> dict[str, dict[str, str]]:
        """{url as written in the brief: page} for each link that could be read."""
        pages = {}
        for url in find_urls(brief):
            page = await self.read(url)
            if page:
                pages[url] = page
        return pages


def brief_with_titles(brief: str, pages: dict[str, dict[str, Any]]) -> str:
    """Search queries and embeddings work on words, not URLs: name each linked page."""
    out = brief
    for url, page in pages.items():
        out = out.replace(url, f'"{page["title"]}" ({url})', 1)
    return out
