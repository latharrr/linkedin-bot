"""API / resource usage: a call ledger (bot_api_calls) plus live provider checks.

Recording: every ApiClient/Researcher HTTP attempt calls `recorder(row)`; the
UsageRecorder writes it to Supabase in the background. Recording never raises
and never blocks the pipeline — a lost ledger row only makes the dashboard
slightly low. Short-lived processes (cron modes, dispatcher) call `drain()`.

Collecting: `collect()` builds the dashboard cards server-side. Provider keys
are used only here; the returned JSON holds numbers and statuses, never keys.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.log import get_logger
from app.spend import spend
from app.timeutil import parse_ts, utcnow

log = get_logger(__name__)

# Free-tier limits that providers don't expose through an API (checked 2026-09-23).
GROQ_TOKENS_PER_DAY = 200_000  # gpt-oss-120b, from Groq's own 429 message; rolling 24 h
NVIDIA_FREE_CREDITS = 1_000  # build.nvidia.com starter credits; ~1 credit per request (estimate)
SUPABASE_FREE_DB_BYTES = 500 * 1024**2  # free plan; shared with the rest of the proofmart project
SUPABASE_FREE_STORAGE_BYTES = 1024**3
LINKEDIN_TOKEN_DAYS = 60
LIVE_TIMEOUT = 15.0


# ── recording ────────────────────────────────────────────────────────────────
def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def call_row(
    provider: str,
    endpoint: str,
    model: str | None,
    resp: httpx.Response | None,
    exc: BaseException | None = None,
    credits: int | None = None,
) -> dict[str, Any]:
    """One ledger row from an HTTP attempt (response or exception)."""
    row: dict[str, Any] = {
        "provider": provider,
        "endpoint": endpoint,
        "model": model,
        "ok": resp is not None and resp.status_code < 400,
        "status": resp.status_code if resp is not None else None,
        "credits": credits,
    }
    if resp is not None:
        h = resp.headers
        row |= {
            "ratelimit_remaining_requests": _int(h.get("x-ratelimit-remaining-requests")),
            "ratelimit_limit_requests": _int(h.get("x-ratelimit-limit-requests")),
            "ratelimit_remaining_tokens": _int(h.get("x-ratelimit-remaining-tokens")),
            "ratelimit_limit_tokens": _int(h.get("x-ratelimit-limit-tokens")),
        }
        if resp.status_code < 400:
            try:
                usage = resp.json().get("usage") or {}
            except (ValueError, AttributeError):
                usage = {}
            row |= {
                "prompt_tokens": _int(usage.get("prompt_tokens")),
                "completion_tokens": _int(usage.get("completion_tokens")),
                "total_tokens": _int(usage.get("total_tokens")),
            }
        else:
            row["error"] = resp.text[:300]
    else:
        row["error"] = type(exc).__name__ if exc else "unknown"
    return {k: v for k, v in row.items() if v is not None}


class UsageRecorder:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, row: dict[str, Any]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._write(row))
        except RuntimeError:  # no running loop: nothing sensible to do
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, row: dict[str, Any]) -> None:
        try:
            await self._db.record_api_call(row)
        except Exception as exc:  # the ledger must never break the pipeline
            log.warning("usage_record_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})

    async def drain(self, timeout: float = 10.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)


# ── summarising the ledger ───────────────────────────────────────────────────
@dataclass
class ProviderStats:
    calls: int = 0
    ok: int = 0
    errors: int = 0
    rate_limited: int = 0
    tokens: int = 0
    credits: int = 0
    by_model: Counter[str] = field(default_factory=Counter)
    last_error: str | None = None
    last_error_at: str | None = None
    latest_headers: dict[str, int] | None = None


def summarize(rows: list[dict[str, Any]]) -> dict[str, ProviderStats]:
    """rows newest-first (as queried) → per-provider stats."""
    stats: dict[str, ProviderStats] = defaultdict(ProviderStats)
    for r in rows:
        s = stats[r["provider"]]
        s.calls += 1
        s.tokens += r.get("total_tokens") or 0
        s.credits += r.get("credits") or 0
        s.by_model[r.get("model") or r.get("endpoint") or "?"] += 1
        if r.get("ok"):
            s.ok += 1
        else:
            s.errors += 1
            s.rate_limited += r.get("status") == 429
            if s.last_error is None:
                s.last_error = f"{r.get('status') or ''} {r.get('error') or ''}".strip()[:200]
                s.last_error_at = r.get("at")
        if s.latest_headers is None and r.get("ratelimit_limit_requests") is not None:
            s.latest_headers = {
                k: r[k]
                for k in ("ratelimit_remaining_requests", "ratelimit_limit_requests", "ratelimit_remaining_tokens", "ratelimit_limit_tokens")
                if r.get(k) is not None
            }
    return dict(stats)


# ── cards ────────────────────────────────────────────────────────────────────
def meter(label: str, used: float | None, limit: float | None, unit: str = "", source: str = "ledger", note: str = "") -> dict[str, Any]:
    return {"label": label, "used": used, "limit": limit, "unit": unit, "source": source, "note": note}


def card(cid: str, name: str, role: str, status: str, meters: list[dict[str, Any]], facts: list[list[str]], **extra: Any) -> dict[str, Any]:
    return {"id": cid, "name": name, "role": role, "status": status, "meters": meters, "facts": facts, **extra}


def _status(configured: bool, s: ProviderStats | None, meters: list[dict[str, Any]]) -> str:
    if not configured:
        return "off"
    if s and s.calls and s.ok == 0:
        return "error"
    if not meters and not (s and s.calls):
        return "idle"  # configured, but nothing to judge by yet
    worst = max(((m["used"] or 0) / m["limit"] for m in meters if m.get("limit")), default=0)
    if worst >= 0.9 or (s and s.rate_limited):
        return "warn"
    return "ok"


def _facts(s: ProviderStats | None) -> list[list[str]]:
    if not s:
        return [["Calls (24 h)", "0"]]
    facts = [["Calls (24 h)", f"{s.calls} ({s.ok} ok, {s.errors} failed)"]]
    if s.rate_limited:
        facts.append(["Rate-limited (24 h)", str(s.rate_limited)])
    top = ", ".join(f"{m.split('/')[-1]} ×{n}" for m, n in s.by_model.most_common(4))
    if top:
        facts.append(["Models", top])
    return facts


def groq_card(
    configured: bool, s: ProviderStats | None, cid: str = "groq", name: str = "Groq",
    spent_usd: float | None = None, cap_usd: float | None = None,
) -> dict[str, Any]:
    h = (s.latest_headers if s else None) or {}
    meters = [meter("Tokens, rolling 24 h", s.tokens if s else 0, GROQ_TOKENS_PER_DAY, "tokens", "ledger", "gpt-oss-120b free tier; browsing ≈ 30–50k per research call")]
    if h.get("ratelimit_limit_requests"):
        limit = h["ratelimit_limit_requests"]
        meters.append(meter("Requests today", limit - h.get("ratelimit_remaining_requests", limit), limit, "req", "headers", "from Groq's last response headers"))
    if h.get("ratelimit_limit_tokens"):
        limit = h["ratelimit_limit_tokens"]
        meters.append(meter("Tokens this minute", limit - h.get("ratelimit_remaining_tokens", limit), limit, "tokens", "headers", "calls are serialised to stay under this"))
    if spent_usd is not None:  # the paid account: money, not free-tier tokens, is what runs out
        meters = [meter("Spend, last 24 h (est.)", round(spent_usd, 4), cap_usd, "USD", "ledger", "tokens × list price; daily cap GROQ2_DAILY_USD")]
    return card(cid, name, "Writer + web research" if cid == "groq" else "Failover writer + web research (second account)", _status(configured, s, meters), meters, _facts(s), last_error=s.last_error if s else None)


def openrouter_card(configured: bool, s: ProviderStats | None, key: dict[str, Any] | None, live_error: str | None) -> dict[str, Any]:
    meters = []
    facts = _facts(s)
    if key:
        free = key.get("free_model_daily_requests") or {}
        if free.get("limit"):
            meters.append(meter("Free-model requests today", free.get("used"), free.get("limit"), "req", "live"))
        facts.append(["Credits used (all time)", f"${key.get('usage', 0)}"])
        facts.append(["Tier", "free (no credits)" if key.get("is_free_tier") else "paid"])
    status = "error" if live_error and configured else _status(configured, s, meters)
    return card("openrouter", "OpenRouter", "Writer fallbacks (free models)", status, meters, facts, last_error=live_error or (s.last_error if s else None))


def nvidia_card(configured: bool, s: ProviderStats | None, lifetime_calls: int | None) -> dict[str, Any]:
    meters = [meter("Credits used (lifetime, est.)", lifetime_calls, NVIDIA_FREE_CREDITS, "credits", "estimate", "~1 credit per request; check build.nvidia.com for the exact balance")]
    return card("nvidia", "NVIDIA NIM", "Embeddings + FLUX images", _status(configured, s, meters), meters, _facts(s), last_error=s.last_error if s else None)


def tavily_card(configured: bool, s: ProviderStats | None, usage: dict[str, Any] | None, live_error: str | None) -> dict[str, Any]:
    meters, facts = [], _facts(s)
    if usage:
        acct = usage.get("account") or {}
        if acct.get("plan_limit"):
            meters.append(meter("Plan credits this month", acct.get("plan_usage"), acct.get("plan_limit"), "credits", "live"))
        facts.append(["Plan", str(acct.get("current_plan", "?"))])
    status = "error" if live_error and configured else _status(configured, s, meters)
    return card("tavily", "Tavily", "Research fallback + 2 PM news topic", status, meters, facts, last_error=live_error or (s.last_error if s else None))


BRIGHTDATA_FREE_RECORDS = 5000


def brightdata_card(configured: bool, s: ProviderStats | None, rows: list[dict[str, Any]], lifetime: int | None, daily_cap: int) -> dict[str, Any]:
    day = sum(int(r.get("credits") or 0) for r in rows if r.get("provider") == "brightdata" and r.get("ok"))
    meters = [
        meter("Free records used (lifetime, est.)", lifetime, BRIGHTDATA_FREE_RECORDS, "records", "estimate", "counts ledgered calls; one-off script scrapes aren't included"),
        meter("Records, last 24 h", day, daily_cap, "records", "ledger", "hard cap per IST day (BRIGHTDATA_DAILY_RECORDS)"),
    ]
    return card("brightdata", "Bright Data", "Fresh research (ChatGPT Search) + X/LinkedIn links", _status(configured, s, meters), meters, _facts(s), last_error=s.last_error if s else None)


NEWSDATA_DAILY_CREDITS = 200


def newsdata_card(configured: bool, s: ProviderStats | None, rows: list[dict[str, Any]]) -> dict[str, Any]:
    day = sum(int(r.get("credits") or 1) for r in rows if r.get("provider") == "newsdata")
    meters = [meter("Credits, last 24 h", day, NEWSDATA_DAILY_CREDITS, "credits", "ledger", "free plan; news is delayed ~12 h")]
    return card("newsdata", "NewsData.io", "News topics (2 PM fallback, /ideas, \"you pick\")", _status(configured, s, meters), meters, _facts(s), last_error=s.last_error if s else None)


def publishing_card(counts: dict[str, int], heartbeat: str | None, next_at: str | None, last_url: str | None, now: datetime) -> dict[str, Any]:
    """Is anything posted, what's scheduled, and is the publisher (dispatcher) running."""
    beat = parse_ts(heartbeat) if heartbeat else None
    running = beat is not None and (now - beat) <= timedelta(minutes=10)
    facts = [
        ["Publisher", ("running, last check " + beat.isoformat(timespec="minutes")) if running else ("NOT RUNNING: approved posts won't publish" if beat is None else f"stopped (last ran {beat.isoformat(timespec='minutes')})")],
        ["Posted", f"{counts.get('posted', 0)}" + (f" (last: {last_url})" if last_url else " (nothing on LinkedIn yet)")],
        ["Scheduled", f"{counts.get('queued', 0)}" + (f" (next {next_at})" if next_at else "")],
        ["Waiting for approval", str(counts.get("awaiting_choice", 0))],
        ["Discarded / failed", str(counts.get("failed", 0))],
    ]
    due_unpublished = counts.get("queued", 0) and not running
    status = "warn" if due_unpublished else ("ok" if running else "idle")
    return card("publishing", "Posts & publishing", "Exactly what is posted and what will be", status, [], facts)


def gateway_card(
    cid: str, name: str, configured: bool, s: ProviderStats | None, hint: str, role: str = "Writer fallbacks (free models)"
) -> dict[str, Any]:
    status = _status(configured, s, [])
    facts = _facts(s)
    if status == "error":
        facts.append(["Action", hint])
    return card(cid, name, role, status, [], facts, last_error=s.last_error if s else None)


def supabase_card(res: dict[str, Any] | None, live_error: str | None) -> dict[str, Any]:
    if not res:
        return card("supabase", "Supabase", "Database + image storage", "error", [], [], last_error=live_error)
    meters = [
        meter("Database size", res.get("db_bytes"), SUPABASE_FREE_DB_BYTES, "bytes", "live", "free plan; the whole proofmart project counts"),
        meter("Image storage", res.get("storage_bytes"), SUPABASE_FREE_STORAGE_BYTES, "bytes", "live", f"{res.get('storage_objects', 0)} images in post-images"),
    ]
    rows = res.get("rows") or {}
    facts = [["Rows", f"{rows.get('posts', 0)} posts · {rows.get('past_posts', 0)} corpus · {rows.get('bot_api_calls', 0)} ledger"]]
    return card("supabase", "Supabase", "Database + image storage", _status(True, None, meters), meters, facts)


def linkedin_card(issued_at: str | None, has_token: bool, now: datetime) -> dict[str, Any]:
    issued = parse_ts(issued_at) if issued_at else None
    if not has_token or issued is None:
        return card("linkedin", "LinkedIn", "Publishing", "off", [], [["Status", "not connected — run scripts/linkedin_auth.py"]])
    age = (now - issued).days
    meters = [meter("Token age", age, LINKEDIN_TOKEN_DAYS, "days", "live", "re-auth before day 60; the bot warns from day 50")]
    return card("linkedin", "LinkedIn", "Publishing", _status(True, None, meters), meters, [["Expires in", f"~{max(LINKEDIN_TOKEN_DAYS - age, 0)} days"]])


def telegram_card(me: dict[str, Any] | None, live_error: str | None) -> dict[str, Any]:
    if not me:
        return card("telegram", "Telegram", "Control chat", "error", [], [], last_error=live_error)
    return card("telegram", "Telegram", "Control chat", "ok", [], [["Bot", f"@{me.get('username')}"], ["Limits", "none that matter at this volume"]])


# ── live checks (server-side; keys never leave this process) ─────────────────
async def _get_json(http: httpx.AsyncClient, url: str, token: str) -> dict[str, Any]:
    resp = await http.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=LIVE_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def _guard(coro: Any) -> tuple[Any, str | None]:
    try:
        return await asyncio.wait_for(coro, LIVE_TIMEOUT + 5), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"


async def collect(settings: Any, db: Any, http: httpx.AsyncClient, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    sec = lambda s: s.get_secret_value() if s else None  # noqa: E731

    async def openrouter_key() -> dict[str, Any]:
        return (await _get_json(http, "https://openrouter.ai/api/v1/key", sec(settings.openrouter_api_key)))["data"]

    async def tavily_usage() -> dict[str, Any]:
        return await _get_json(http, "https://api.tavily.com/usage", sec(settings.tavily_api_key))

    async def telegram_me() -> dict[str, Any]:
        resp = await http.get(f"https://api.telegram.org/bot{sec(settings.telegram_bot_token)}/getMe", timeout=LIVE_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["result"]

    async def nothing() -> None:
        return None

    (rows, rows_err), (lifetime, _), (bd_lifetime, _), (res, res_err), (or_key, or_err), (tav, tav_err), (me, me_err), (li_tok, _), (li_at, _) = await asyncio.gather(
        _guard(db.api_calls_since(now - timedelta(hours=24))),
        _guard(db.count_api_calls("nvidia")),
        _guard(db.count_api_calls("brightdata")),
        _guard(db.resource_usage()),
        _guard(openrouter_key() if settings.openrouter_api_key else nothing()),
        _guard(tavily_usage() if settings.tavily_api_key else nothing()),
        _guard(telegram_me()),
        _guard(db.get_setting("linkedin_access_token")),
        _guard(db.get_setting("linkedin_token_issued_at")),
    )
    stats = summarize(rows or [])
    chat_id = getattr(settings, "my_chat_id", 0)
    (counts, _), (beat, _), (queued, _), (posted, _) = await asyncio.gather(
        _guard(db.status_counts(chat_id)) if hasattr(db, "status_counts") else _guard(nothing()),
        _guard(db.get_setting("dispatcher_last_run")),
        _guard(db.posts_with_status(chat_id, "queued", 1)) if hasattr(db, "posts_with_status") else _guard(nothing()),
        _guard(db.last_posted(1)) if hasattr(db, "last_posted") else _guard(nothing()),
    )
    next_at = queued[0].scheduled_at.isoformat(timespec="minutes") if queued and queued[0].scheduled_at else None
    last_url = posted[0].post_url if posted else None
    cards = [
        publishing_card(counts or {}, beat, next_at, last_url, now),
        groq_card(bool(settings.groq_api_key), stats.get("groq")),
        *([groq_card(True, stats.get("groq2"), "groq2", "Groq (paid account)", spend(rows or [], "groq2"), getattr(settings, "groq2_daily_usd", None))]
          if getattr(settings, "groq_api_key_2", None) else []),
        openrouter_card(bool(settings.openrouter_api_key), stats.get("openrouter"), or_key, or_err),
        tavily_card(bool(settings.tavily_api_key), stats.get("tavily"), tav, tav_err),
        nvidia_card(bool(settings.nvidia_api_key), stats.get("nvidia"), lifetime),
        brightdata_card(bool(settings.brightdata_api_key), stats.get("brightdata"), rows or [], bd_lifetime,
                        getattr(settings, "brightdata_daily_records", 10)),
        gateway_card("jev", "Jev (TypeSafe AI)", bool(getattr(settings, "jev_api_key", None)), stats.get("jev"),
                     "rate-limited or down; the bot falls back to the chat model's decision",
                     role="Second opinion before chat-started drafts"),
        newsdata_card(bool(getattr(settings, "newsdata_api_key", None)), stats.get("newsdata"), rows or []),
        gateway_card("modelscope", "ModelScope", bool(settings.modelscope_api_key), stats.get("modelscope"),
                     "link an Alibaba Cloud account at modelscope.ai → Settings → Account"),
        gateway_card("zenmux", "ZenMux", bool(settings.zenmux_api_key), stats.get("zenmux"),
                     "key is pay-as-you-go; free models return 403 — check key type/balance"),
        supabase_card(res, res_err),
        linkedin_card(li_at, bool(li_tok), now),
        telegram_card(me, me_err),
    ]
    return {"generated_at": now.isoformat(timespec="seconds"), "ledger_error": rows_err, "cards": cards}


# ── Telegram /dashboard (plain text; parse_mode is never set) ───────────────
STATUS_ICON = {"ok": "🟢", "warn": "🟡", "error": "🔴", "idle": "⚪", "off": "⚫"}


def _num(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def meter_line(m: dict[str, Any]) -> str:
    used, limit, unit = m.get("used"), m.get("limit"), m.get("unit") or ""
    if unit == "bytes":  # show storage in MB
        used = None if used is None else used / 1_048_576
        limit = None if limit is None else limit / 1_048_576
        unit = "MB"
    if unit == "USD":  # money: dollars and cents, never rounded to one decimal
        spent = f"${used or 0:.3f}"
        return f"{m['label']}: {spent} of ${limit:.2f} cap · ${max(limit - (used or 0), 0):.2f} left" if limit else f"{m['label']}: {spent}"
    if limit:
        left = max(limit - (used or 0), 0)
        pct = f" ({(used or 0) / limit:.0%})" if used is not None else ""
        return f"{m['label']}: {_num(used)} / {_num(limit)} {unit}{pct} · {_num(left)} left"
    return f"{m['label']}: {_num(used)} {unit}".rstrip()


def cards_text(data: dict[str, Any]) -> list[str]:
    """One Telegram message per ~group of cards: used vs left for every API."""
    head = "📊 API usage — " + str(data.get("generated_at", ""))[:16].replace("T", " ") + " UTC"
    blocks = [head + ("\n⚠ usage ledger unavailable: " + data["ledger_error"] if data.get("ledger_error") else "")]
    for c in data.get("cards", []):
        lines = [f"{STATUS_ICON.get(c['status'], '•')} {c['name']} — {c['role']}"]
        lines += [f"   {meter_line(m)}" for m in c.get("meters", [])]
        lines += [f"   {k}: {v}" for k, v in c.get("facts", [])[:3]]
        if c["status"] == "error" and c.get("last_error"):
            lines.append(f"   last error: {str(c['last_error'])[:120]}")
        blocks.append("\n".join(lines))
    out, cur = [], ""
    for b in blocks:  # stay under Telegram's 4096-char message limit
        if len(cur) + len(b) + 2 > 3800:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    return [*out, cur] if cur else out
