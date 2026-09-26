"""Regressions from the 2026-09-26 session: rules he states hold, ideas never repeat,
news ideas trace to a fetched source."""

import asyncio
from datetime import UTC, datetime

import pytest

import bot as botmod
from app.constraints import NO_STORY_NOTE, Constraints
from app.timeutil import IST
from bot import BotHandler
from tests.conftest import CHAT_ID

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)
BACKGROUND = [
    "Strategic intern in PicaPool's Founder's Office since Jul 2025; built an event-fingerprinting attribution system",
    "Growth & Marketing Intern in UniLyf's Founder's Office (UK pre-launch campus social platform)",
]


@pytest.fixture
def h(svc, fakes):
    fakes["db"].voice["background"] = list(BACKGROUND)
    return BotHandler(svc, now=lambda: NOW)


async def drain(h):
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


def idea(pillar, text, hook, news=None):
    return {"pillar": pillar, "idea": text, "hook": hook, "news": news, "fits_niche": True}


def shown_text(fakes):
    return next(t for t in reversed(fakes["messenger"].texts()) if t.startswith("💡 Ideas for you"))


class FakeNews:
    """NewsData.latest: articles per keyword, or an exception."""

    def __init__(self, articles=None, fail=False):
        self.articles = articles or {}
        self.fail = fail
        self.asked: list[str] = []

    async def latest(self, keyword, field="q", exact=True):
        self.asked.append(keyword)
        if self.fail:
            raise RuntimeError("newsdata down")
        return self.articles.get(keyword, [])


def article(title, link, date="2026-09-22 06:10:00", source="The Ken"):
    return {"title": title, "link": link, "pubDate": date, "source_name": source, "description": "…"}


# ── 1. rules he states hold until he lifts them ──────────────────────────────
async def test_keep_my_personal_story_out_is_acknowledged_and_persists(h, fakes, svc):
    await h.on_text(CHAT_ID, "keep my personal story out for now")
    assert fakes["messenger"].texts()[-1].startswith("Got it: no personal stories.")
    assert fakes["nvidia"].chat_calls == []
    restarted = BotHandler(svc, now=lambda: NOW)  # survives a restart (settings table)
    assert (await restarted.get_rules(CHAT_ID)).no_personal
    await restarted.on_text(CHAT_ID, "ok personal stories are fine again")
    assert not (await restarted.get_rules(CHAT_ID)).no_personal


async def test_no_personal_ideas_never_pitch_his_own_work(h, fakes):
    """Live bug: he said "keep my personal story out", then got PicaPool attribution and
    UniLyf growth hacks pitched."""
    await h.on_text(CHAT_ID, "keep my personal story out for now")
    fakes["nvidia"].ideas_reply = [
        idea("*", "How PicaPool's attribution system found the real channel.", "One poster beat the whole ad budget........"),
        idea("*", "The UniLyf growth hacks that filled a pre-launch community.", "200 students joined before the app existed........"),
        idea("*", "Race conditions explain why two teams both thought the other owned onboarding.", "Two teams, one flow, zero owners........"),
    ]
    await h.on_text(CHAT_ID, "/ideas")
    text = shown_text(fakes)
    assert "PicaPool" not in text and "UniLyf" not in text and "Race conditions" in text
    prompt = fakes["nvidia"].idea_prompts[-1]
    assert "no personal stories" in prompt  # the model is told up front too
    assert "\n1. personal\n" not in prompt.split("in this order:\n", 1)[1] + "\n"  # no Personal-stakes slot while it's off


async def test_chat_reply_that_breaks_the_rule_is_retried_then_replaced(h, fakes):
    await h.on_text(CHAT_ID, "keep my personal story out for now")
    fakes["nvidia"].chat_reply = [
        {"reply": "Sure, we'll skip personal anecdotes. How about your PicaPool attribution system?", "draft_topic": None},
        {"reply": "Or your UniLyf growth hacks?", "draft_topic": None},
    ]
    await h.on_text(CHAT_ID, "what could I write about this week, any thoughts")
    last = fakes["messenger"].texts()[-1]
    assert "PicaPool" not in last and "UniLyf" not in last
    assert last.startswith("Keeping to your rules (no personal stories")
    assert "broke his standing rule" in fakes["nvidia"].chat_calls_history[-1][-1]["content"]


async def test_chat_retry_that_complies_is_used(h, fakes):
    await h.on_text(CHAT_ID, "keep my personal story out for now")
    fakes["nvidia"].chat_reply = [
        {"reply": "How about your PicaPool attribution system?", "draft_topic": None},
        {"reply": "How about why eval costs quietly beat model costs?", "draft_topic": None},
    ]
    await h.on_text(CHAT_ID, "what could I write about this week, any thoughts")
    assert fakes["messenger"].texts()[-1] == "How about why eval costs quietly beat model costs?"


async def test_drafts_carry_the_rule_and_the_gate_enforces_it(h, fakes, svc, monkeypatch):
    briefs: list[str] = []

    async def fake_run_brief(svc, chat_id, brief, on_stage=None, **_):
        briefs.append(brief)

    monkeypatch.setattr(botmod, "run_brief", fake_run_brief)
    await h.on_text(CHAT_ID, "no personal stories please")
    await h.on_text(CHAT_ID, "make a post about eval costs")
    await drain(h)
    assert briefs == ["eval costs" + NO_STORY_NOTE]

    from app.pipeline import Ungrounded, run_brief

    fakes["nvidia"].final_text = "I built a KPI digest at PicaPool that nobody read.\n\nEval costs are the real bill.\n\nP.S. What does yours cost?"
    with pytest.raises(Ungrounded) as exc:
        await run_brief(svc, CHAT_ID, "eval costs" + NO_STORY_NOTE)
    assert exc.value.problems["I built a KPI digest at PicaPool that nobody read."] == "a personal story, and you asked to keep those out"


async def test_no_politics_filters_ideas(h, fakes):
    await h.on_text(CHAT_ID, "no politics")
    fakes["nvidia"].ideas_reply = [
        idea("*", "Election-season political ads show why caching decisions goes wrong.", "The ad ran after the vote........"),
        idea("*", "Fingerprinting signups back to a channel beat the install counter.", "Our best channel was a poster........"),
    ]
    await h.on_text(CHAT_ID, "/ideas")
    text = shown_text(fakes)
    assert "political" not in text and "Fingerprinting" in text


def test_constraint_phrases():
    c = Constraints()
    assert c.update("no problem, thanks") == [] and not c
    assert c.update("avoid crypto posts") == ["no crypto"]
    assert c.update("crypto is fine again") == ["crypto is back in"] and not c


# ── 2. ideas already shown are never shown again ─────────────────────────────
async def test_second_ask_never_repeats_the_first_batch(h, fakes):
    """Live bug: "something what happened today in tech" returned the same three ideas
    shown two minutes earlier."""
    fakes["nvidia"].ideas_reply = [
        idea("*", "Local LLM docs are eating Stack Overflow answers.", "Nobody asked the forum this week........"),
        idea("*", "Fingerprinting signups back to a channel beat the install counter.", "Our best channel was a poster........"),
    ]
    await h.on_text(CHAT_ID, "/ideas")
    first = shown_text(fakes)
    await h.on_text(CHAT_ID, "/ideas")  # the model pitches the very same ideas again
    assert fakes["messenger"].texts()[-1].startswith("Nothing new that fits right now")
    assert shown_text(fakes) == first  # no second batch of the same ideas
    assert "Fingerprinting signups back to a channel" in fakes["nvidia"].idea_prompts[-1].split("ALREADY SHOWN", 1)[1]


async def test_reworded_repeat_is_dropped_but_new_ideas_pass(h, fakes):
    fakes["nvidia"].ideas_reply = [idea("*", "Race conditions explain why two teams both thought the other owned onboarding.", "Two teams, zero owners........")]
    await h.on_text(CHAT_ID, "/ideas")
    fakes["nvidia"].ideas_reply = [
        idea("*", "Race conditions explain why both teams thought the other one owned onboarding.", "Nobody owned onboarding........"),
        idea("*", "Fingerprinting signups back to a channel beat the install counter.", "Our best channel was a poster........"),
    ]
    await h.on_text(CHAT_ID, "/ideas")
    text = shown_text(fakes)
    assert "Nobody owned onboarding" not in text and "Fingerprinting" in text


async def test_more_interesting_is_a_new_fetch_not_a_chat_turn(h, fakes):
    h.svc.news = FakeNews()
    await h.on_text(CHAT_ID, "/ideas")
    first_keywords = list(h.svc.news.asked)
    await h.on_text(CHAT_ID, "something more interesting?")
    assert not any("Telegram assistant inside" in str(c["messages"]) for c in fakes["nvidia"].chat_calls)  # no chat model turn
    assert len(fakes["nvidia"].idea_prompts) == 2
    assert h.svc.news.asked[len(first_keywords):] != first_keywords  # other keywords: a genuinely new fetch


# ── 3. news ideas trace to a fetched source ──────────────────────────────────
async def test_news_ideas_show_their_source_url_and_date(h, fakes):
    h.svc.news = FakeNews({k: [article("Rapido's CEO says bike taxis now break even", "https://the-ken.com/rapido")] for k in ("AI agents", "startups", "edtech")})
    fakes["nvidia"].ideas_reply = [
        idea("*", "Break-even bike taxis are a caching story: the fleet is the cache.", "The fleet was the cache all along........", news=1),
    ]
    await h.on_text(CHAT_ID, "what happened today in tech industry")
    text = shown_text(fakes)
    assert "📰 Rapido's CEO says bike taxis now break even (The Ken, 2026-09-22) https://the-ken.com/rapido" in text
    assert "[1] Rapido's CEO says bike taxis now break even" in fakes["nvidia"].idea_prompts[-1]


@pytest.mark.parametrize("cited", ["Gemini TTS ships in India", 7])
async def test_ideas_citing_a_headline_that_wasnt_fetched_are_dropped(h, fakes, cited):
    h.svc.news = FakeNews({"AI agents": [article("Local LLM docs are eating Stack Overflow", "https://example.com/llm")]})
    fakes["nvidia"].ideas_reply = [
        idea("*", "Gemini TTS shows speech is the new UI layer.", "Nobody typed a word........", news=cited),
        idea("*", "Fingerprinting signups back to a channel beat the install counter.", "Our best channel was a poster........"),
    ]
    await h.on_text(CHAT_ID, "/ideas")
    text = shown_text(fakes)
    assert "Gemini" not in text and "Fingerprinting" in text


@pytest.mark.parametrize("news", [FakeNews(fail=True), FakeNews()])
async def test_news_ask_with_a_failed_or_empty_fetch_says_so(h, fakes, news):
    h.svc.news = news
    await h.on_text(CHAT_ID, "what happened today in tech industry")
    last = fakes["messenger"].texts()[-1]
    assert ("couldn't fetch" in last or "came back empty" in last) and "I won't make headlines up" in last
    assert fakes["nvidia"].idea_prompts == []  # no model call that could invent them


async def test_an_article_already_used_is_not_fetched_again(h, fakes):
    h.svc.news = FakeNews({k: [article("Rapido's CEO says bike taxis now break even", "https://the-ken.com/rapido")] for k in ("AI agents", "startups", "edtech")})
    fakes["nvidia"].ideas_reply = [idea("*", "Bike taxis as a cache.", "The fleet was the cache........", news=1)]
    await h.on_text(CHAT_ID, "what happened today in tech industry")
    await h.on_text(CHAT_ID, "what's new in the news today?")
    last = fakes["messenger"].texts()[-1]
    assert "came back with nothing new" in last and "I won't make headlines up" in last
