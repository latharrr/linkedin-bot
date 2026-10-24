"""Post ideas, filtered through his niche and pillars (pillars.toml).

The pillar slots come from the weighted rotation (never the same pillar twice in a row);
today's headlines are only raw material; the model writes one idea + curiosity-gap hook
per slot, and anything off-niche, mis-tagged or opening on a thesis is dropped.

Grounded like drafts: a news idea must cite a headline this run actually fetched (title,
source, date, URL), by number; one that doesn't is dropped, and a fetch that fails or
comes back empty is said out loud, never papered over. Ideas already shown in this chat,
and the articles behind them, are excluded, so "more" is always a new fetch; his standing
constraints (app/constraints.py) filter every pitch."""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from typing import Any

from app.constraints import Constraints
from app.log import get_logger
from app.news import _JUNK_TITLE, _is_wire
from app.pillars import (
    LAST_PILLAR,
    Pillar,
    Pillars,
    load_pillars,
    no_repeats,
    plan_pillars,
)
from app.pipeline import load_bank, load_voice, optional, with_bank
from app.services import Services
from app.style_check import HOOK_MAX, is_thesis_hook
from app.timeutil import IST, utcnow
from app.writer import unbold

log = get_logger("ideas")
IDEA_PREFIX = "Idea:"  # an idea brief is a pitch, not his own account (see writer.is_own_story)


@dataclass(frozen=True)
class Headline:
    """One article a fetch actually returned."""

    title: str
    url: str
    date: str  # YYYY-MM-DD: the article's publish date, or the day it was fetched when the source gives none
    source: str

    def cite(self) -> str:
        return f"{self.title} ({self.source}, {self.date}) {self.url}"


@dataclass(frozen=True)
class Idea:
    pillar: Pillar
    idea: str
    hook: str
    news: Headline | None = None

    def text(self) -> str:
        """As shown to him: the pillar tag, the hook, the idea, and its source if it's news."""
        return f"{self.pillar.tag} “{self.hook}”\n{self.idea}" + (f"\n📰 {self.news.cite()}" if self.news else "")

    def brief(self) -> str:
        """What the drafting pipeline gets when he taps ✍️ Draft."""
        parts = [f"{IDEA_PREFIX} {self.idea}", f"Suggested hook: {self.hook}", f"Pillar: {self.pillar.name}"]
        return "\n".join(parts + ([f"News it builds on: {self.news.cite()}"] if self.news else []))


@dataclass
class Batch:
    ideas: list[Idea]
    note: str | None = None  # said to him: a failed or empty news fetch, or nothing new left
    headlines: list[Headline] = field(default_factory=list)


def validate_ideas(raw: list[dict[str, Any]], pillars: Pillars, slots: list[str], news: list[Headline] | None = None) -> list[Idea]:
    """Keep ideas that carry one of the planned pillar tags, stay in the niche, open on a
    curiosity gap of at most HOOK_MAX characters, and cite only headlines that were fetched."""
    news = news or []
    out = []
    for item in raw:
        pillar = pillars.get(str(item.get("pillar") or "").strip())
        idea = " ".join(unbold(str(item.get("idea") or "")).split())
        hook = " ".join(unbold(str(item.get("hook") or "")).split()).strip("\"'“” ")
        cited = item.get("news")
        index = int(cited) if isinstance(cited, int | float) or (isinstance(cited, str) and cited.strip().isdigit()) else None
        why = (
            "pillar" if pillar is None or pillar.key not in slots
            else "off_niche" if item.get("fits_niche") is False
            else "empty" if not idea or not hook
            else "hook_length" if len(hook) > HOOK_MAX
            else "thesis_hook" if is_thesis_hook(hook)
            else "ungrounded_news" if cited not in (None, "", "null", "None") and not (index and 1 <= index <= len(news))
            else None
        )
        if why:
            log.info("idea_dropped", extra={"reason": why, "pillar": str(item.get("pillar"))[:30]})
            continue
        out.append(Idea(pillar, idea, hook, news[index - 1] if index else None))  # type: ignore[arg-type]
    return out


def _headline(article: dict[str, Any], fetched: str) -> Headline | None:
    if not article.get("title") or not article.get("link") or article.get("duplicate") or _is_wire(article) or _JUNK_TITLE.search(str(article["title"])):
        return None
    date = str(article.get("pubDate") or "")[:10] or fetched
    return Headline(" ".join(str(article["title"]).split()), str(article["link"]), date, str(article.get("source_name") or article.get("source_id") or "news"))


_TOPIC_STRING = re.compile(r"^(?P<title>.+?) — .*\(niche: [^;]*; source: (?P<url>\S+)\)$", re.S)
DEFAULT_KEYWORDS = ["AI", "startups", "technology"]
PER_FETCH = 2


async def fetch_headlines(svc: Services, n: int, offset: int = 0, seen: set[str] | frozenset[str] = frozenset()) -> tuple[list[Headline], str | None]:
    """Up to n fresh articles for the niche keywords, skipping URLs already used in this
    chat. offset moves to other keywords, so a second ask is a genuinely new fetch.
    Returns (headlines, a note for him when the fetch failed or found nothing new)."""
    keywords = svc.settings.keywords or DEFAULT_KEYWORDS
    day = utcnow().astimezone(IST)
    fetched = day.date().isoformat()
    width = min(PER_FETCH, len(keywords))  # a few keywords per fetch, rotating, so the next ask reads others
    picks = [keywords[(day.toordinal() + offset * width + i) % len(keywords)] for i in range(width)]
    where = "qInTitle" if offset % 2 == 0 else "q"  # headline matches first, then the wider body match
    found: list[Headline] = []
    errors = 0
    try:
        if svc.news is not None:
            batches = await asyncio.gather(*(svc.news.latest(k, where) for k in picks), return_exceptions=True)
            for articles in batches:
                if isinstance(articles, BaseException):
                    errors += 1
                    continue
                found += [h for a in articles if (h := _headline(a, fetched)) is not None]
        else:
            topic = await svc.researcher.news_topic(keywords, day.toordinal() + offset)
            m = _TOPIC_STRING.match(topic or "")
            if m:
                found.append(Headline(m["title"].strip(), m["url"], fetched, "web search"))
    except Exception as exc:
        log.warning("headlines_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:160]})
        return [], "I couldn't fetch today's news just now (the news source failed)"
    fresh = list({h.url: h for h in found if h.url not in seen}.values())[:n]
    log.info("headlines", extra={"keywords": picks, "found": len(found), "fresh": len(fresh), "errors": errors})
    if fresh:
        return fresh, None
    if errors and not found:
        return [], "I couldn't fetch today's news just now (the news source failed)"
    return [], "The news fetch came back with nothing new" if found else "The news fetch came back empty"


def _content(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in {"that", "this", "with", "from", "what", "your", "have", "they", "when", "story", "work"}}


def already_shown(idea: Idea, shown: list[Idea]) -> bool:
    """The same article, or an idea that's mostly the same words (a rephrased repeat)."""
    mine = _content(f"{idea.idea} {idea.hook}")
    for old in shown:
        if idea.news and old.news and idea.news.url == old.news.url:
            return True
        theirs = _content(f"{old.idea} {old.hook}")
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= 0.5:
            return True
    return False


async def pitch_ideas(
    svc: Services, n: int, rng: random.Random | None = None, *, shown: list[Idea] | None = None,
    constraints: Constraints | None = None, offset: int = 0, news_only: bool = False,
) -> Batch:
    """n ideas (fewer if some fail the checks), each tagged with its pillar, in an order
    where no pillar follows itself, starting away from the last one he drafted.
    shown: ideas already pitched in this chat (never again, not even rephrased).
    news_only: "what happened today": every idea must build on a fetched headline."""
    shown, constraints = shown or [], constraints or Constraints()
    pillars = load_pillars()
    last = await optional(lambda: svc.db.get_setting(LAST_PILLAR), None, "last_pillar")
    keys = [p for p in pillars.pillars if not (constraints.no_personal and p.key == "personal")]
    planning = Pillars(pillars.niche, tuple(keys)) if keys else pillars
    slots = [p.key for p in plan_pillars(planning, n, last, rng)]
    voice = with_bank(await load_voice(svc), await load_bank(svc))
    seen = {i.news.url for i in shown if i.news}
    news, note = await fetch_headlines(svc, n, offset, seen)
    if news_only and not news:
        return Batch([], f"{note or 'No fresh news'}, so I have no news-based ideas to show. I won't make headlines up.")
    avoid = [f"{i.hook} / {i.idea}" for i in shown]
    raw = await svc.writer.ideas(slots, [h.cite() for h in news], voice, avoid=avoid, rules=constraints.prompt_text(), news_only=news_only)
    facts = [str(f) for f in voice.get("background") or []]
    ideas = []
    for idea in validate_ideas(raw, pillars, slots, news):
        why = ("repeat" if already_shown(idea, shown + ideas)
               else "no_news" if news_only and idea.news is None
               else constraints.breaks(f"{idea.hook} {idea.idea}", facts, who="idea"))
        if why:
            log.info("idea_dropped", extra={"reason": why, "pillar": idea.pillar.key})
            continue
        ideas.append(idea)
    log.info("ideas", extra={"slots": slots, "kept": len(ideas), "news": len(news), "shown_before": len(shown)})
    ideas = no_repeats(ideas, lambda i: i.pillar.key, last)
    if not ideas:
        return Batch([], (note + ". " if note else "") + "Nothing new that fits right now: every idea I got was a repeat or broke a rule you set.", news)
    return Batch(ideas, f"{note}, so these come from your pillars only." if note and not news_only else None, news)
