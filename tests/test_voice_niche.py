"""Phase 2: niche, content pillars (pillars.toml) and his voice rules."""

import asyncio
import random
from collections import Counter
from datetime import UTC, datetime

import pytest

import bot as botmod
from app.pillars import load_pillars, plan_pillars
from app.style_check import draft_violations, fixable_issues, ps_ending, voice_issues
from app.timeutil import IST
from bot import BotHandler
from tests.conftest import CHAT_ID

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)


# ── pillars.toml ─────────────────────────────────────────────────────────────
def test_pillars_file_carries_his_niche_and_five_weighted_pillars():
    p = load_pillars()
    assert p.niche.startswith("a student founder's-office operator who builds AI and growth systems in public")
    assert {x.key: x.weight for x in p.pillars} == {"build": 30, "attribution": 25, "cs_metaphor": 20, "personal": 15, "ai_war_story": 10}


def test_rotation_never_repeats_a_pillar_back_to_back_and_follows_the_weights():
    p = load_pillars()
    rng = random.Random(7)
    counts: Counter[str] = Counter()
    last = "build"
    for _ in range(2000):
        plan = plan_pillars(p, 3, last, rng)
        assert plan[0].key != last and all(a.key != b.key for a, b in zip(plan, plan[1:], strict=False))
        counts.update(x.key for x in plan)
        last = plan[-1].key
    order = [k for k, _ in counts.most_common()]
    assert order == ["build", "attribution", "cs_metaphor", "personal", "ai_war_story"]  # heavier pillars come up more


def test_weights_are_editable_without_code(tmp_path):
    f = tmp_path / "pillars.toml"
    f.write_text('niche = "x"\n[[pillar]]\nkey = "a"\nname = "A"\nweight = 1\n[[pillar]]\nkey = "b"\nname = "B"\nweight = 9\n')
    counts = Counter(p.key for p in plan_pillars(load_pillars(f), 1000, None, random.Random(1)))
    assert counts["a"] + counts["b"] == 1000 and abs(counts["a"] - counts["b"]) <= 2  # alternation wins over weights
    bad = tmp_path / "bad.toml"  # load_pillars is cached per file: the bot reads it once per start
    bad.write_text('niche = "x"\n[[pillar]]\nkey = "a"\nname = "A"\nweight = 0\n')
    with pytest.raises(ValueError):
        load_pillars(bad)


# ── voice rules ──────────────────────────────────────────────────────────────
GOOD = (
    "Our best channel was a poster nobody tracked........\n\n"
    "I found it the week I fingerprinted every signup.\n\n"
    "Ads got the credit. The poster did the work.\n\n"
    "P.S. Which channel of yours never gets credit?"
)


def test_a_draft_that_follows_his_voice_has_no_violations():
    assert draft_violations(GOOD) == []


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (GOOD.replace("Our best channel was a poster nobody tracked........", "Why attribution is the key to growth."), "thesis hook"),
        (GOOD.replace("Ads", "𝐀𝐝𝐬"), "bold-Unicode"),
        (GOOD + "\n\n#a #b #c #d #e", "hashtag dump"),
        (GOOD.replace("Ads", "Ads 🚀🔥💯"), "emoji spam"),
        (GOOD.replace("P.S. Which channel of yours never gets credit?", "That's it."), "no closing question"),
    ],
)
def test_each_voice_rule_is_caught(text, rule):
    assert rule in draft_violations(text)


def test_personal_stake_is_real_and_early_or_omitted():
    late = "Most teams can't see their channels.\n\nDashboards lie.\n\nInstalls are vanity.\n\nI fixed ours with fingerprinting.\n\nP.S. Yours?"
    assert "no personal stake" in draft_violations(late)  # his line comes after the lesson
    omitted = "Most teams can't see their channels........\n\nDashboards lie.\n\nP.S. Which one lies to you?"
    assert "no personal stake" not in draft_violations(omitted)  # no story is allowed; an invented one is not


def test_rules_the_code_enforces_never_cost_a_model_fix_pass():
    issues = fixable_issues(GOOD.replace("Ads", "𝐀𝐝𝐬").replace("P.S. Which channel of yours never gets credit?", "Done."))
    assert not any(i.startswith(("Remove the bold-Unicode", "End with a genuine question")) for i in issues)
    assert voice_issues("x")  # still reported, just fixed in code (unbold, ps_ending)


@pytest.mark.parametrize(
    ("text", "question", "ending"),
    [
        ("Line one.\n\nWhich channel lies to you?", "", "P.S. Which channel lies to you?"),
        ("Line one.\n\nDone.", "Which channel lies to you?", "Done.\n\nP.S. Which channel lies to you?"),
        ("Line one.\n\nP.S. Already fine?\n\n#growth", "", "P.S. Already fine?\n\n#growth"),
    ],
)
def test_every_draft_ends_on_a_ps_question(text, question, ending):
    assert ps_ending(text, question).endswith(ending)


async def test_generated_drafts_end_on_ps_and_carry_no_bold(svc, fakes):
    from app.pipeline import run_brief

    fakes["nvidia"].final_text = "𝐎𝐮𝐫 best channel was a poster........\n\nAds got the credit.\n\nWhich channel never gets credit?"
    post = await run_brief(svc, CHAT_ID, "channel attribution")
    assert post.draft_a.splitlines()[-1] == "P.S. Which channel never gets credit?"
    assert "𝐎𝐮𝐫" not in post.draft_a and post.draft_a.startswith("Our best channel")


# ── niche filter ─────────────────────────────────────────────────────────────
@pytest.fixture
def h(svc):
    return BotHandler(svc, now=lambda: NOW)


async def drain(h):
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


async def test_off_niche_brief_is_refused_with_a_draft_anyway_button(h, fakes, monkeypatch):
    from tests.fakes import OUTLINE_JSON

    monkeypatch.setitem(OUTLINE_JSON, "niche_fit", False)
    monkeypatch.setitem(OUTLINE_JSON, "niche_note", "celebrity gossip has no pillar")
    await h.on_text(CHAT_ID, "draft: celebrity gossip")
    await drain(h)
    last = fakes["messenger"].texts()[-1]
    assert last.startswith("I didn't draft this: it's outside your niche (celebrity gossip has no pillar)")
    assert fakes["messenger"].keyboards()[-1] == [[("✍️ Draft anyway", "niche:go")]]
    assert fakes["db"].posts == {}
    forced: list[bool] = []

    async def fake_run_brief(svc, chat_id, brief, on_stage=None, force=False):
        forced.append(force)

    monkeypatch.setattr(botmod, "run_brief", fake_run_brief)
    await h.on_callback(CHAT_ID, "cb", "niche:go", message_id=3)
    await drain(h)
    assert forced == [True]
