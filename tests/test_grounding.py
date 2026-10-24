"""P0: the grounding hard gate, the no-repeat rule and clarify-don't-guess."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import bot as botmod
from app.grounding import (
    Problems,
    claims_about_him,
    lifted_lines,
    mark_unverified,
    placeholder_lines,
    repeated_claims,
)
from app.pipeline import Ungrounded
from app.timeutil import IST
from bot import NO_STORY_NOTE, POST_NOW_BUTTON_ONLY, YES_TO_WHAT, BotHandler
from tests.conftest import CHAT_ID

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)
PROFILE = (
    "Growth was judged on total downloads with no channel-level attribution, so he built an event-fingerprinting "
    "attribution system across on-ground and digital funnels, then shipped 15+ production internal tools in 8 weeks "
    "(CRM with role-based access, WhatsApp API dashboard, campaign attribution, merchant onboarding)."
)
NAMES = {"PicaPool"}


@pytest.fixture
def h(svc):
    return BotHandler(svc, now=lambda: NOW)


async def drain(h):
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


def buttons(fakes):
    return [cb for row in fakes["messenger"].keyboards()[-1] for _, cb in row]


# ── deterministic checks ─────────────────────────────────────────────────────
def test_profile_paragraph_lifted_into_a_draft_is_caught():
    post = (
        "Nobody could tell which channel worked.\n\n"
        "So I shipped 15+ production internal tools in 8 weeks, CRM with role-based access included.\n\n"
        "P.S. What do you measure first?"
    )
    assert lifted_lines(post, [PROFILE]) == ["So I shipped 15+ production internal tools in 8 weeks, CRM with role-based access included."]


def test_a_reframed_topical_line_is_not_a_lift():
    post = "The founders asked which poster brought the last 40 signups. I had no answer that week.\n\nP.S. How do you track offline?"
    assert lifted_lines(post, [PROFILE]) == []


def test_reusing_a_recent_metric_or_story_is_a_repeat():
    recent = ["I shipped 15+ internal tools in eight weeks at PicaPool."]
    post = (
        "Fifteen tools taught me one thing.\n\n"
        "At PicaPool I built 15+ of them before anyone asked for a roadmap.\n\n"
        "My laptop was never once the reason something didn't ship.\n\n"
        "P.S. What did you build first?"
    )
    assert repeated_claims(post, recent, NAMES) == ["At PicaPool I built 15+ of them before anyone asked for a roadmap."]
    story = ["It was never once the reason something didn't ship, I told my family."]
    assert repeated_claims(post, story, NAMES) == ["My laptop was never once the reason something didn't ship."]


def test_naming_his_employer_or_a_year_again_is_not_a_lift_or_repeat():
    recent = ["In July 2025 I joined PicaPool's Founder's Office."]
    post = "In 2025 I learned what a KPI digest costs at PicaPool's Founder's Office.\n\nP.S. What's yours?"
    assert repeated_claims(post, recent, NAMES) == []
    assert lifted_lines(post, ["Strategic intern in PicaPool's Founder's Office since Jul 2025"]) == []


def test_third_party_stats_are_not_his_claims():
    line = "Anthropic's February 2026 analysis found only 0.8% of tool calls were irreversible."
    assert claims_about_him(line, {"PicaPool", "Feb", "AST"}) == []


def test_a_different_real_story_is_not_a_repeat():
    recent = ["I shipped 15+ internal tools in eight weeks at PicaPool."]
    post = "My KPI digest replaced a 40-minute morning report.\n\nP.S. What report would you kill?"
    assert repeated_claims(post, recent, NAMES) == []
    assert claims_about_him(post, NAMES) == ["My KPI digest replaced a 40-minute morning report."]


def test_blanks_only_he_can_fill_are_caught():
    post = "Use my code [your referral code] for credits.\nOr sign up at <link>.\nThis line is fine [verify] 47%."
    assert placeholder_lines(post) == ["Use my code [your referral code] for credits.", "Or sign up at <link>."]


def test_unbacked_third_party_numbers_are_tagged_verify_not_stated():
    post = "73% of founders skip attribution.\nKPMG says 47% of startups fail.\nI cut my report time by 90%."
    out = mark_unverified(post, "KPMG: 47% of startups fail", NAMES)
    assert out.splitlines() == ["73% [verify] of founders skip attribution.", "KPMG says 47% of startups fail.", "I cut my report time by 90%."]


def test_problems_text_names_each_line_and_why():
    p = Problems()
    p.add(["I raised $2M."], "a number about you nothing backs")
    p.add(["I raised $2M."], "copied from your profile")  # the first reason stays
    assert p.text() == "• I raised $2M. (a number about you nothing backs)"


# ── the gate in the pipeline ─────────────────────────────────────────────────
async def test_a_story_used_in_the_last_14_days_blocks_the_draft(svc, fakes):
    from app.pipeline import run_brief

    fakes["db"].voice["background"] = ["Shipped 15+ production internal tools in 8 weeks at PicaPool"]
    fakes["db"].add_post(status="posted", created_at=datetime.now(UTC) - timedelta(days=3), research={"claims_used": ["I shipped 15+ internal tools in 8 weeks at PicaPool."]})
    fakes["nvidia"].final_text = "I shipped 15+ internal tools in 8 weeks, and none of them mattered.\n\nThe lesson came later.\n\nP.S. What would you cut?"
    with pytest.raises(Ungrounded) as exc:
        await run_brief(svc, CHAT_ID, "why internal tools beat roadmaps")
    assert exc.value.problems["I shipped 15+ internal tools in 8 weeks, and none of them mattered."] == "already used in a post from the last 14 days"
    outline_prompt = next(c for c in fakes["nvidia"].chat_calls if "Return ONLY JSON" in c["messages"][-1]["content"])
    assert "used_in_last_14_days_do_not_reuse" in outline_prompt["messages"][-1]["content"]  # the writer is told up front


async def test_old_posts_outside_the_window_dont_count(svc, fakes):
    from app.pipeline import recent_claims

    fakes["db"].add_post(status="posted", created_at=datetime.now(UTC) - timedelta(days=20), research={"claims_used": ["old"]})
    fakes["db"].add_post(status="queued", created_at=datetime.now(UTC) - timedelta(days=2), research={"claims_used": ["new"]})
    fakes["db"].add_post(status="awaiting_choice", created_at=datetime.now(UTC), research={"claims_used": ["draft"]})
    assert await recent_claims(svc, facts=[]) == ["new"]


async def test_a_clean_draft_logs_its_claims_for_the_no_repeat_rule(svc, fakes):
    from app.pipeline import run_brief

    fakes["nvidia"].final_text = "My KPI digest replaced a morning report.\n\nThat was the whole trick.\n\nP.S. What report would you kill?"
    post = await run_brief(svc, CHAT_ID, "automated KPI digests")
    assert post.research["claims_used"] == ["My KPI digest replaced a morning report."]


# ── the bot asks instead of inventing ────────────────────────────────────────
@pytest.fixture
def blocked(monkeypatch):
    """run_brief blocks the first time, then records the brief it was given."""
    briefs: list[str] = []

    async def fake_run_brief(svc, chat_id, brief, on_stage=None, **_):
        briefs.append(brief)
        if len(briefs) == 1:
            problems = Problems()
            problems.add(["At my last job I cut churn by 30%."], "not backed by your brief or profile")
            raise Ungrounded(problems)

    monkeypatch.setattr(botmod, "run_brief", fake_run_brief)
    return briefs


async def test_ungrounded_draft_asks_for_his_real_detail_and_uses_his_answer(h, fakes, blocked):
    await h.on_text(CHAT_ID, "draft: churn in campus apps")
    await drain(h)
    ask = fakes["messenger"].texts()[-1]
    assert ask.startswith("✋ I held this draft back instead of guessing.")
    assert "• At my last job I cut churn by 30%. (not backed by your brief or profile)" in ask
    assert "YOUR POST" not in "".join(fakes["messenger"].texts())
    await h.on_text(CHAT_ID, "At PicaPool half the signups from one fest never opened the app again")
    await drain(h)
    assert len(blocked) == 2
    assert blocked[1].startswith("churn in campus apps\n\nHis own experience with this")
    assert blocked[1].endswith("half the signups from one fest never opened the app again")


async def test_no_personal_story_button_drafts_without_one(h, fakes, blocked):
    await h.on_text(CHAT_ID, "draft: churn in campus apps")
    await drain(h)
    none = next(cb for cb in buttons(fakes) if cb.startswith("ask:none:"))
    await h.on_callback(CHAT_ID, "cb", none, message_id=7)
    await drain(h)
    assert blocked[1] == "churn in campus apps" + NO_STORY_NOTE


async def test_drop_it_and_a_typed_no_save_nothing(h, fakes, blocked):
    await h.on_text(CHAT_ID, "draft: churn in campus apps")
    await drain(h)
    await h.on_text(CHAT_ID, "no")
    assert fakes["messenger"].texts()[-1] == "Okay — dropped it. Nothing was saved."
    assert len(blocked) == 1 and h.asks == {}


async def test_a_question_while_asked_is_not_taken_as_the_answer(h, fakes, blocked):
    await h.on_text(CHAT_ID, "draft: churn in campus apps")
    await drain(h)
    await h.on_text(CHAT_ID, "what's queued?")
    assert len(blocked) == 1 and CHAT_ID in h.asks


async def test_referral_code_is_asked_for_never_invented(h, fakes, monkeypatch):
    briefs: list[str] = []

    async def fake_run_brief(svc, chat_id, brief, on_stage=None, **_):
        briefs.append(brief)

    monkeypatch.setattr(botmod, "run_brief", fake_run_brief)
    await h.on_text(CHAT_ID, "draft: Groq's free tier and add my referral code")
    assert fakes["messenger"].texts()[-1] == "What's your referral code? Send it exactly as it should appear — I won't make one up."
    assert briefs == []
    await h.on_text(CHAT_ID, "GROQ-DL-2026")
    await drain(h)
    assert briefs == ["Groq's free tier and add my referral code\n\nHis referral code, to use exactly as given (never change or invent it): GROQ-DL-2026"]


# ── clarify, don't guess ─────────────────────────────────────────────────────
async def test_topic_that_matches_one_draft_needs_no_question(h, fakes):
    fakes["db"].add_post(draft_b="", topic="eval costs for agents")
    fakes["db"].add_post(draft_b="", topic="agent memory")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "discard", "post": None, "about": "the eval costs one"}}
    await h.on_text(CHAT_ID, "drop the eval costs one")
    assert fakes["messenger"].texts()[-1].startswith("Discard this? It will never post.")


async def test_topic_that_matches_several_drafts_gets_a_pick_list_of_those(h, fakes):
    fakes["db"].add_post(draft_b="", topic="agent evals")
    fakes["db"].add_post(draft_b="", topic="agent memory")
    fakes["db"].add_post(draft_b="", topic="stablecoins")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "discard", "post": None, "about": "the agent draft"}}
    await h.on_text(CHAT_ID, "discard the agent draft")
    text = fakes["messenger"].texts()[-1]
    assert text.startswith("Which one should I discard?") and "stablecoins" not in text
    assert sum(cb.startswith("act:pick:") for cb in buttons(fakes)) == 3  # 2 posts + "All 2 drafts"


async def test_topic_that_matches_nothing_says_so_and_never_invents_a_ref(h, fakes, monkeypatch):
    fakes["db"].add_post(draft_b="", topic="eval costs")
    fakes["db"].add_post(draft_b="", topic="agent memory")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "schedule", "post": "the crypto post", "when": "09:00"}}
    await h.on_text(CHAT_ID, "schedule the crypto post for 9")
    assert "I don't see a draft about “the crypto post”." in fakes["messenger"].texts()
    assert h.pending == {}
    assert buttons(fakes)[0] == "menu:/drafts"
    started: list[str] = []

    async def fake_run_brief(svc, chat_id, brief, on_stage=None, **_):
        started.append(brief)

    monkeypatch.setattr(botmod, "run_brief", fake_run_brief)
    await h.on_callback(CHAT_ID, "cb", buttons(fakes)[1])
    await drain(h)
    assert started == ["the crypto post"]


async def test_ambiguous_discard_with_several_drafts_asks_the_scope(h, fakes):
    a = fakes["db"].add_post(draft_b="", topic="eval costs")
    b = fakes["db"].add_post(draft_b="", topic="agent memory")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "discard", "post": None}}
    await h.on_text(CHAT_ID, "discard it")
    assert fakes["messenger"].texts()[-1].startswith("Which one should I discard?")
    labels = [label for row in fakes["messenger"].keyboards()[-1] for label, _ in row]
    assert labels[-1] == "🗑 All 2 drafts"
    assert fakes["db"].posts[a.id]["status"] == fakes["db"].posts[b.id]["status"] == "awaiting_choice"


async def test_yes_with_nothing_pending_asks_what_he_means(h, fakes):
    await h.on_text(CHAT_ID, "yes")
    assert fakes["messenger"].texts()[-1] == YES_TO_WHAT
    assert fakes["nvidia"].chat_calls == []  # no model turn, no guess
    assert buttons(fakes) == ["menu:/status", "menu:/drafts", "menu:/queue"]


async def test_yes_after_the_bot_asked_something_goes_to_the_model(h, fakes):
    fakes["nvidia"].chat_reply = {"reply": "Want an angle on agent memory?", "draft_topic": None}
    await h.on_text(CHAT_ID, "any ideas?")
    fakes["nvidia"].chat_reply = {"reply": "Great, here's one.", "draft_topic": None}
    await h.on_text(CHAT_ID, "yes")
    assert fakes["messenger"].texts()[-1] == "Great, here's one."


async def test_post_it_now_typed_in_chat_is_refused(h, fakes):
    post = fakes["db"].add_post(draft_b="")
    fakes["nvidia"].chat_reply = {"reply": "", "action": {"name": "schedule", "post": post.id[:8], "when": "now"}}
    await h.on_text(CHAT_ID, "post it now")
    assert fakes["messenger"].texts()[-1] == POST_NOW_BUTTON_ONLY
    assert h.pending == {} and fakes["db"].posts[post.id]["status"] == "awaiting_choice"


async def test_a_draft_with_verify_tags_cannot_be_scheduled(h, fakes):
    from app.telegram_ui import build_callback

    post = fakes["db"].add_post(draft_b="", chosen="a", draft_a="73% [verify] of founders skip it.\n\nP.S. Do you?")
    await h.on_callback(CHAT_ID, "cb", build_callback("time", post.id, "9am"))
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice"
    assert "[verify]" in fakes["messenger"].texts()[-1]
