"""Bright Data Scrapers Library (billed per record; 5,000 free records on this account).

Used sparingly, only where the free sources fall short:
  - ChatGPT Search (web search on): one record per draft run, for fresh facts with
    citations. Its facts are then grounded against the cited pages (fetched for free),
    exactly like the Groq browsing research.
  - X and LinkedIn post URLs pasted into a brief: a plain fetch can't read them.

Every call is capped per IST day (BRIGHTDATA_DAILY_RECORDS) and written to the usage ledger.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.log import get_logger
from app.timeutil import ist_day_start, utcnow
from app.usage import call_row

log = get_logger(__name__)

API = "https://api.brightdata.com/datasets/v3"
CHATGPT_SEARCH = "gd_m7aof0k82r803d5bjm"  # ChatGPT Search — search by prompt
X_POSTS = "gd_lwxkxvnf1cynvib9co"  # X (Twitter) posts — collect by URL
LINKEDIN_POSTS = "gd_lyy3tktm25m4avu764"  # LinkedIn posts — collect by URL
DATASET_NAMES = {CHATGPT_SEARCH: "chatgpt_search", X_POSTS: "x_posts", LINKEDIN_POSTS: "linkedin_posts"}

SYNC_TIMEOUT = 90.0  # after this Bright Data answers 202 + snapshot_id and we poll
DEADLINE = 180.0  # total per scrape, polling included (ChatGPT search took ~65 s live)
POLL_SECONDS = 5.0
TEXT_KEYS = ("description", "post_text", "text", "content", "title")


class BrightDataError(RuntimeError):
    pass


class BudgetExhausted(BrightDataError):
    pass


class BrightData:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        db: Any,
        daily_records: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._db = db
        self._daily = daily_records
        self._sleep = sleep
        self._lock = asyncio.Lock()  # budget check + spend are one step
        self.recorder: Any = None

    async def used_today(self) -> int:
        rows = await self._db.api_calls_since(ist_day_start(utcnow()))
        return sum(int(r.get("credits") or 1) for r in rows if r.get("provider") == "brightdata" and r.get("ok"))

    def _record(self, dataset: str, resp: httpx.Response | None, exc: BaseException | None, records: int) -> None:
        if self.recorder is None:
            return
        try:
            row = call_row("brightdata", DATASET_NAMES.get(dataset, dataset), None, resp, exc, credits=records)
            self.recorder(row)
        except Exception:  # recording must never break a call
            log.warning("usage_record_skipped", extra={"endpoint": "brightdata"})

    async def scrape(self, dataset: str, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self._lock:
            used = await self.used_today()
            if used + len(inputs) > self._daily:
                raise BudgetExhausted(f"Bright Data daily cap reached ({used}/{self._daily} records today)")
            try:
                records = await asyncio.wait_for(self._scrape(dataset, inputs), timeout=DEADLINE)
            except TimeoutError as exc:
                self._record(dataset, None, exc, 0)
                raise BrightDataError(f"{DATASET_NAMES.get(dataset, dataset)}: no result within {DEADLINE:.0f}s") from exc
        return records

    async def _scrape(self, dataset: str, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        params = {"dataset_id": dataset, "include_errors": "true", "notify": "false"}
        try:
            resp = await self._http.post(
                f"{API}/scrape", params=params, headers=self._headers, json={"input": inputs}, timeout=SYNC_TIMEOUT
            )
        except httpx.HTTPError as exc:
            self._record(dataset, None, exc, 0)
            raise BrightDataError(f"scrape: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            self._record(dataset, resp, None, 0)
            raise BrightDataError(f"scrape: HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if resp.status_code == 202 or (isinstance(body, dict) and body.get("snapshot_id") and "message" in body):
            body = await self._wait_for(body["snapshot_id"])
        records = body if isinstance(body, list) else [body]
        self._record(dataset, resp, None, len(inputs))
        return [r for r in records if isinstance(r, dict)]

    async def _wait_for(self, snapshot: str) -> Any:
        while True:
            prog = await self._http.get(f"{API}/progress/{snapshot}", headers=self._headers, timeout=30)
            status = prog.json().get("status") if prog.status_code == 200 else None
            if status == "ready":
                break
            if status == "failed":
                raise BrightDataError(f"snapshot {snapshot} failed")
            await self._sleep(POLL_SECONDS)
        data = await self._http.get(f"{API}/snapshot/{snapshot}", params={"format": "json"}, headers=self._headers, timeout=60)
        if data.status_code != 200:
            raise BrightDataError(f"snapshot download: HTTP {data.status_code}")
        return data.json()

    # ── dataset helpers ──────────────────────────────────────────────────────
    async def chatgpt_search(self, prompt: str) -> dict[str, Any]:
        records = await self.scrape(
            CHATGPT_SEARCH,
            [{"url": "https://chatgpt.com/", "prompt": prompt, "country": "", "web_search": True, "require_sources": True, "additional_prompt": ""}],
        )
        rec = next((r for r in records if not r.get("error")), None)
        if rec is None:
            raise BrightDataError(f"chatgpt_search: {records[0].get('error') if records else 'no record'}")
        return rec

    async def social_post(self, url: str) -> dict[str, str] | None:
        """Text of an X or LinkedIn post by URL → {title, url, content}; None if unsupported."""
        dataset = social_dataset(url)
        if dataset is None:
            return None
        records = await self.scrape(dataset, [{"url": url, "country": ""}] if dataset == X_POSTS else [{"url": url}])
        rec = next((r for r in records if not r.get("error")), None)
        text = next((rec[k] for k in TEXT_KEYS if rec and isinstance(rec.get(k), str) and rec[k].strip()), None) if rec else None
        if not text:
            return None
        author = str((rec or {}).get("user_posted") or (rec or {}).get("user_id") or urlsplit(url).netloc)
        return {"title": f"Post by {author}", "url": url, "content": text.strip()}


_SOCIAL = {"x.com": X_POSTS, "twitter.com": X_POSTS, "mobile.twitter.com": X_POSTS, "linkedin.com": LINKEDIN_POSTS}


def social_dataset(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    dataset = _SOCIAL.get(host)
    if dataset == LINKEDIN_POSTS and "/posts/" not in url and "/feed/update/" not in url:
        return None  # profiles, jobs etc. are not posts
    if dataset == X_POSTS and "/status/" not in url:
        return None
    return dataset


# ── ChatGPT Search answer → grounded research facts ─────────────────────────
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_HAS_DIGIT = re.compile(r"\d")
_REF = re.compile(r"\\?\[(\d{1,3})\\?\]")


def clean_url(url: str) -> str:
    """Drop utm_* tracking (ChatGPT adds utm_source=chatgpt.com to every citation)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def answer_facts(record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """→ (candidate facts [{claim, urls}], {cited url: title}). Each list item of the answer
    that states a number is one candidate. Its sources are the [n] markers in it
    (n = links_attached position) or inline markdown links."""
    answer = str(record.get("answer_text_markdown") or record.get("answer_text") or "")
    by_position = {
        int(link["position"]): clean_url(link["url"])
        for link in record.get("links_attached") or []
        if isinstance(link, dict) and link.get("url") and str(link.get("position") or "").isdigit()
    }
    cited: dict[str, str] = {}
    for c in (record.get("links_attached") or []) + (record.get("citations") or []) + (record.get("search_sources") or []):
        if isinstance(c, dict) and c.get("url"):
            title = str(c.get("title") or c.get("text") or "").strip()
            cited.setdefault(clean_url(c["url"]), title)
    facts = []
    for block in (b.strip() for b in _ITEM.split(answer)):
        refs = [by_position[int(n)] for n in _REF.findall(block) if int(n) in by_position]
        links = [clean_url(u) for _, u in _MD_LINK.findall(block)]
        text = _REF.sub("", _MD_LINK.sub(r"\1", block)).replace("**", "").replace("__", "")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 30 or text.endswith(":") or not _HAS_DIGIT.search(text):
            continue  # the intro line ("Here are 6 facts:") or a number-free aside
        facts.append({"claim": text[:600], "urls": list(dict.fromkeys(refs + links))})
    return facts, cited
