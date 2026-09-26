"""The draft buttons (✂️ 🎣 🔥 / 🖼 / 🗑 / 👀 / ↩️ / ⚡), the commands and quick menu,
/dashboard formatting, and the mobile-shape step (length, hook, paragraph splitting)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.pipeline import fit_shape
from app.style_check import HOOK_MAX, airy, check_draft, post_hook
from app.telegram_ui import (
    MAX_CALLBACK_BYTES,
    build_callback,
    draft_keyboard,
    draft_tools,
    guard_allows,
    parse_callback,
    queued_keyboard,
    time_keyboard,
)
from app.timeutil import IST
from app.usage import cards_text, meter_line
from bot import HELP, MENU, BotHandler
from tests.conftest import CHAT_ID

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)


@pytest.fixture
def h(svc):
    return BotHandler(svc, now=lambda: NOW)


async def drain(h):
    while h.tasks:
        await asyncio.gather(*list(h.tasks))


async def tap(h, action, post_id, arg=None):
    await h.on_callback(CHAT_ID, "cb", build_callback(action, post_id, arg))


# ── callback data + guards ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("action", "arg"),
    [("tune", f"{w}-{m}") for w in "ab" for m in ("short", "hook", "bold")]
    + [("img", None), ("drop", None), ("show", None), ("unq", None), ("asap", None)],
)
def test_new_callbacks_round_trip_under_64_bytes(action, arg):
    pid = "4b07d4f3-d27f-41ae-bd9c-680351adac32"
    data = build_callback(action, pid, arg)
    assert len(data.encode()) <= MAX_CALLBACK_BYTES
    cb = parse_callback(data)
    assert (cb.action, cb.arg, cb.post_id) == (action, arg, pid)


def test_bad_tune_arg_rejected():
    assert parse_callback("tune:a-longer:4b07d4f3-d27f-41ae-bd9c-680351adac32") is None


@pytest.mark.parametrize(
    ("action", "ok_status", "bad_status"),
    [("tune", "awaiting_choice", "queued"), ("img", "awaiting_choice", "posted"), ("show", "awaiting_choice", "queued"),
     ("unq", "queued", "awaiting_choice"), ("asap", "queued", "posting"), ("drop", "queued", "posted")],
)
def test_new_guards(action, ok_status, bad_status):
    cb = parse_callback(build_callback(action, "4b07d4f3-d27f-41ae-bd9c-680351adac32", "a-short" if action == "tune" else None))
    assert guard_allows(cb, ok_status, None) and not guard_allows(cb, bad_status, None)


def test_keyboards_carry_the_hotkeys():
    pid = "4b07d4f3-d27f-41ae-bd9c-680351adac32"
    labels = lambda kb: [label for row in kb for label, _ in row]  # noqa: E731
    assert labels(draft_tools(pid, "a")) == ["✂️ Shorter", "🎣 New hook", "🔥 Bolder", "✅ Post this", "✏️ Edit"]
    assert labels(draft_keyboard(pid)) == ["🔄 New version", "🖼 New image", "🗑 Discard"]
    # older two-draft rows keep their A/B controls
    assert labels(draft_tools(pid, "b", single=False))[3:] == ["✅ Post B", "✏️ Edit B"]
    assert labels(draft_keyboard(pid, two=True))[:2] == ["✅ Post A", "✅ Post B"]
    assert labels(queued_keyboard(pid)) == ["⚡ Post now", "↩️ Unschedule", "🗑 Discard"]


# ── tune buttons ─────────────────────────────────────────────────────────────
async def test_tune_rewrites_one_draft_and_resets_the_pick(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "tune", post.id, "a-hook")
    await drain(h)
    row = fakes["db"].posts[post.id]
    assert row["draft_a"].startswith("Sharper hook.") and row["draft_b"] == "draft B text"
    assert row["chosen"] is None and row["edited_a"] is False
    last_text, last_kb = fakes["messenger"].sent[-1][2]
    assert last_text.startswith("DRAFT A") and last_kb == draft_tools(post.id, "a", single=False)  # older two-draft row
    prompt = fakes["nvidia"].tune_prompts[0]
    assert "Rewrite ONLY the hook" in prompt and "Candidate hooks" in prompt  # outline hooks offered
    assert post.id not in h.regenerating


async def test_tune_that_adds_an_unverified_number_is_rejected(h, fakes):
    post = fakes["db"].add_post()
    fakes["nvidia"].tune_reply = "93% of founders agree.\n\ndraft A text"
    await tap(h, "tune", post.id, "a-bold")
    await drain(h)
    assert fakes["db"].posts[post.id]["draft_a"] == "draft A text"
    assert "can't verify" in fakes["messenger"].texts()[-1]


async def test_tune_blocked_while_busy_and_after_lock(h, fakes):
    post = fakes["db"].add_post()
    h.regenerating.add(post.id)
    await tap(h, "tune", post.id, "b-short")
    assert fakes["messenger"].answers()[-1] == "Working on it — wait for the update"
    h.regenerating.clear()
    fakes["db"].posts[post.id]["status"] = "queued"
    await tap(h, "tune", post.id, "b-short")
    assert fakes["messenger"].answers()[-1] == "Expired" and fakes["nvidia"].tune_prompts == []


# ── image / discard / show ───────────────────────────────────────────────────
async def test_new_image_replaces_both_urls_and_sends_one_photo(h, fakes):
    post = fakes["db"].add_post()
    await tap(h, "img", post.id)
    await drain(h)
    row = fakes["db"].posts[post.id]
    assert row["image_a_url"] == row["image_b_url"] != "https://storage.example/a.png"
    assert [k for k, _, _ in fakes["messenger"].sent].count("photo") == 1
    assert len(fakes["nvidia"].image_calls) == 1


async def test_discard_waiting_and_queued_posts(h, fakes):
    waiting, queued = fakes["db"].add_post(), fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW)
    await tap(h, "drop", waiting.id)
    await tap(h, "drop", queued.id)
    for p in (waiting, queued):
        assert fakes["db"].posts[p.id]["status"] == "failed" and fakes["db"].posts[p.id]["error"] == "discarded by user"
    await tap(h, "pick", waiting.id, "a")  # a discarded post's buttons are dead
    assert fakes["messenger"].answers()[-1] == "Expired"


async def test_show_resends_the_preview(h, fakes):
    post = fakes["db"].add_post()
    await tap(h, "show", post.id)
    kinds = fakes["messenger"].kinds()
    assert kinds.count("photo") == 2 and fakes["messenger"].texts()[-1] == "Pick a draft:"  # legacy row: two urls


# ── queued post buttons ─────────────────────────────────────────────────────
async def test_unschedule_returns_to_time_picker(h, fakes):
    post = fakes["db"].add_post(status="queued", chosen="b", scheduled_at=NOW + timedelta(hours=8))
    await tap(h, "unq", post.id)
    row = fakes["db"].posts[post.id]
    assert row["status"] == "awaiting_choice" and row["scheduled_at"] is None and row["chosen"] == "b"
    assert fakes["messenger"].keyboards()[-1] == time_keyboard(post.id)
    await tap(h, "time", post.id, "6pm")  # gate 2 again works
    assert fakes["db"].posts[post.id]["status"] == "queued"


async def test_post_now_moves_schedule_up(h, fakes):
    post = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=8))
    await tap(h, "asap", post.id)
    assert fakes["db"].posts[post.id]["scheduled_at"] == NOW + timedelta(minutes=2)
    assert fakes["db"].posts[post.id]["status"] == "queued"


async def test_queue_confirmation_has_buttons(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, "9am")
    assert fakes["messenger"].keyboards()[-1] == queued_keyboard(post.id)


# ── commands + quick menu ───────────────────────────────────────────────────
async def test_help_sends_text_and_quick_menu(h, fakes):
    await h.on_text(CHAT_ID, "/start")
    assert fakes["messenger"].texts() == [HELP]
    kind, _, (text, rows) = fakes["messenger"].sent[-1]
    assert kind == "menu" and rows == MENU


async def test_new_without_topic_asks_even_with_chat_context(h, fakes):
    await h.on_text(CHAT_ID, "hey")
    await h.on_text(CHAT_ID, "✍️ New post")
    assert fakes["messenger"].texts()[-1].startswith("What should the post be about?")
    assert fakes["db"].chat_states[CHAT_ID]["pending_action"] == "awaiting_brief"


async def test_new_with_topic_drafts(h, fakes):
    await h.on_text(CHAT_ID, "/new why interns should ship internal tools")
    await drain(h)
    assert next(iter(fakes["db"].posts.values()))["brief"] == "why interns should ship internal tools"


async def test_drafts_and_queue_lists(h, fakes):
    fakes["db"].add_post(topic="Agents need approval gates", created_at=NOW)
    q = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=3), draft_a="Hook line here.\n\nBody")
    await h.on_text(CHAT_ID, "📝 Drafts")
    assert "Agents need approval gates" in fakes["messenger"].texts()[-1]
    await h.on_text(CHAT_ID, "📅 Queue")
    text = fakes["messenger"].texts()[-1]
    assert text.startswith("📅") and "Draft A" in text and "Hook line here." in text
    assert fakes["messenger"].keyboards()[-1] == queued_keyboard(q.id)


async def test_empty_lists(h, fakes):
    await h.on_text(CHAT_ID, "/drafts")
    await h.on_text(CHAT_ID, "/queue")
    assert fakes["messenger"].texts() == ["No drafts waiting. Send /new <topic>.", "Nothing scheduled."]


async def test_menu_works_mid_edit_and_keeps_the_pending_reply(h, fakes):
    post = fakes["db"].add_post()
    await fakes["db"].set_chat_state(CHAT_ID, "awaiting_edit_a", post.id, NOW)
    await h.on_text(CHAT_ID, "📅 Queue")
    assert fakes["messenger"].texts()[-1] == "Nothing scheduled."
    assert fakes["db"].chat_states[CHAT_ID]["pending_action"] == "awaiting_edit_a"
    assert fakes["db"].posts[post.id]["draft_a"] == "draft A text"


class FakeNews:
    def __init__(self, topics):
        self.topics = topics
        self.asked = []

    async def topic(self, keyword):
        self.asked.append(keyword)
        return self.topics.get(keyword)


async def test_ideas_offer_draft_buttons_and_start_a_run(h, fakes, monkeypatch):
    monkeypatch.setattr(h.svc.settings, "niche_keywords", "AI agents,startup,developer tools")
    h.svc.news = FakeNews({
        "AI agents": "Why Everyone Is Talking About Jev — the AI that doesn't chat (niche: AI agents; source: https://forbes.com/jev)",
        "startup": "One Facebook for every startup failure? — VC math (niche: startup; source: https://x.com/vc)",
    })
    await h.on_text(CHAT_ID, "💡 Ideas")
    text, kb = fakes["messenger"].sent[-1][2]
    assert "1. Why Everyone Is Talking About Jev" in text and "(niche:" not in text
    assert kb == [[("✍️ Draft 1", "idea:1"), ("✍️ Draft 2", "idea:2")]]
    await h.on_callback(CHAT_ID, "cb", "idea:1")
    await drain(h)
    assert next(iter(fakes["db"].posts.values()))["brief"].startswith("Why Everyone Is Talking About Jev")


async def test_idea_buttons_expire_after_restart(h, fakes):
    await h.on_callback(CHAT_ID, "cb", "idea:2")
    assert fakes["messenger"].answers() == ["Expired — run /ideas again"] and fakes["db"].posts == {}


async def test_dashboard_command_sends_usage(h, fakes, monkeypatch):
    async def fake_collect(settings, db, http, now=None):
        return {"generated_at": "2026-09-23T08:22:00", "cards": [
            {"id": "groq", "name": "Groq", "role": "Writer", "status": "warn",
             "meters": [{"label": "Tokens, rolling 24 h", "used": 150000, "limit": 200000, "unit": "tokens"}], "facts": []},
        ]}

    monkeypatch.setattr("app.usage.collect", fake_collect)
    await h.on_text(CHAT_ID, "📊 Dashboard")
    texts = fakes["messenger"].texts()
    assert texts[0].startswith("📋 Status")  # "is it posted?" comes first
    assert texts[1].startswith("📊 Checking")
    assert "🟡 Groq — Writer" in texts[2] and "150,000 / 200,000 tokens (75%) · 50,000 left" in texts[2]


async def test_dashboard_tool_text_has_no_extra_status_or_progress_lines(h, fakes, monkeypatch):
    """Unlike /dashboard, the chat-tool version is just the usage cards: the model already
    has a separate "status" tool for "is it posted?", and there's no chat to send progress to."""

    async def fake_collect(settings, db, http, now=None):
        return {"generated_at": "2026-09-23T08:22:00", "cards": [
            {"id": "groq", "name": "Groq", "role": "Writer", "status": "ok", "meters": [], "facts": []},
        ]}

    monkeypatch.setattr("app.usage.collect", fake_collect)
    text = await h.dashboard_tool_text()
    assert "🟢 Groq — Writer" in text and "📋 Status" not in text and "Checking every API" not in text


async def test_ideas_tool_text_lists_topics_without_niche_tag_or_buttons(h, fakes, monkeypatch):
    monkeypatch.setattr(h.svc.settings, "niche_keywords", "AI agents,startup")
    h.svc.news = FakeNews({"AI agents": "Why Everyone Is Talking About Jev (niche: AI agents; source: https://forbes.com/jev)"})
    text = await h.ideas_tool_text()
    assert text == "1. Why Everyone Is Talking About Jev" and CHAT_ID not in h.ideas


async def test_ideas_tool_text_without_keywords_configured(h, fakes, monkeypatch):
    monkeypatch.setattr(h.svc.settings, "niche_keywords", "")
    assert "NICHE_KEYWORDS" in await h.ideas_tool_text()


async def test_posts_tool_text_matches_the_queue_and_drafts_commands(h, fakes):
    assert await h.posts_tool_text(CHAT_ID, "queued") == "Nothing scheduled."
    assert await h.posts_tool_text(CHAT_ID, "awaiting_choice") == "No drafts waiting."
    post = fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=1), draft_a="A sharp hook.\n\nBody")
    text = await h.posts_tool_text(CHAT_ID, "queued")
    assert text.startswith("- ") and "Draft A: A sharp hook." in text and str(post.id) not in text  # no internal ids leak into chat


# ── dashboard text ───────────────────────────────────────────────────────────
def test_meter_line_formats_storage_in_mb():
    line = meter_line({"label": "Image storage", "used": 2 * 1_048_576, "limit": 1024 * 1_048_576, "unit": "bytes"})
    assert line == "Image storage: 2 / 1,024 MB (0%) · 1,022 left"


def test_cards_text_splits_long_output():
    card = {"id": "x", "name": "X", "role": "r" * 300, "status": "ok", "meters": [], "facts": []}
    parts = cards_text({"generated_at": "t", "cards": [card] * 30})
    assert len(parts) > 1 and all(len(p) <= 3800 for p in parts)


# ── mobile shape ─────────────────────────────────────────────────────────────
LONG_PARA = ("I built a bot that drafts my posts. It researches, writes two versions, and makes an image. "
             "But it refuses to post until I tap twice, and it flags every number it can't trace to a source.")


def test_airy_splits_hook_and_dense_paragraphs_without_changing_words():
    text = f"{LONG_PARA}\n\n{LONG_PARA} {LONG_PARA}\n\n- one\n- two\n\nWhat would you gate?"
    out = airy(text)
    assert out.split() == text.split()  # same words, same order
    assert len(post_hook(out)) <= HOOK_MAX
    assert all(len(p) <= 220 for p in out.split("\n\n"))
    assert "- one\n- two" in out


def test_airy_leaves_a_good_post_alone():
    good = "Short hook.\n\nOne line.\n\nWhat would you do?"
    assert airy(good) == good


class ScriptedWriter:
    def __init__(self, replies):
        self.replies = replies
        self.modes = []

    async def tune(self, text, mode, hooks=None):
        self.modes.append(mode)
        return self.replies[mode](text)


async def test_fit_shape_tightens_long_drafts():
    long = "\n\n".join(["A line that says something useful here."] * 50)  # ~2,000 chars
    w = ScriptedWriter({"short": lambda t: "\n\n".join(t.split("\n\n")[:25])})
    out = await fit_shape(w, long, corpus="", hooks=[])
    assert w.modes == ["short"] and len(out) < 1100


async def test_fit_shape_keeps_original_when_tightening_adds_numbers():
    long = "\n\n".join(["A line that says something useful here."] * 50)
    w = ScriptedWriter({"short": lambda t: "73% of people agree.\n\nShort."})
    assert await fit_shape(w, long, corpus="", hooks=[]) == long


async def test_fit_shape_new_hook_only_when_splitting_cannot_fix_it():
    one_long_sentence = "When I started building the bot I assumed the hard part would be the writing but it turned out to be the waiting and the checking of every single claim."
    body = f"{one_long_sentence}\n\nSecond line.\n\nWhat would you check?"
    w = ScriptedWriter({"hook": lambda t: "The hard part wasn't writing.\n\n" + t.split("\n\n", 1)[1]})
    out = await fit_shape(w, body, corpus="", hooks=["The hard part wasn't writing."])
    assert w.modes == ["hook"] and post_hook(out) == "The hard part wasn't writing."
    w2 = ScriptedWriter({})
    assert await fit_shape(w2, f"{LONG_PARA}\n\nWhat would you gate?", corpus="", hooks=[]) != LONG_PARA
    assert w2.modes == []  # sentence split fixed the hook with no model call


def test_style_check_reports_length_hook_and_density():
    report = check_draft(("x" * 300) + "\n\nWhat now?")
    assert any("hook is" in s for s in report.soft) and any("characters (target" in s for s in report.soft)
    assert any("over 220 characters" in s for s in report.soft)


# ── LinkedIn formatting (no markdown renders there) ─────────────────────────
def test_markdown_bold_becomes_unicode_bold_and_keycaps_become_numbers():
    from app.writer import clean_post, unbold

    out = clean_post("**Lesson:** Replace friction.  \n\n1️⃣ Identify it\n2️⃣ Build it\n\n## Why\n\n\n\nWhat now? #ops")
    assert "**" not in out and "1️⃣" not in out and "## " not in out and "  \n" not in out
    assert unbold(out) == "Lesson: Replace friction.\n\n1. Identify it\n2. Build it\n\nWhy\n\nWhat now? #ops"
    assert out.startswith("\U0001d5df")  # 𝗟: sans-serif bold L, which LinkedIn displays


def test_folded_bold_setting_strips_markdown_to_plain():
    from app.writer import clean_post

    assert clean_post("**Lesson:** keep it.", fold_bold=True) == "Lesson: keep it."


def test_style_check_flags_markdown_keycaps_and_bracketed_citations():
    report = check_draft("Hook.\n\n**Lesson:** x\n\n1️⃣ step\n\n63% said so (Zapier/Centiment, March 2026).\n\nWhat now?")
    assert "emoji used as a bullet" in report.hard
    assert any("markdown" in h for h in report.hard)
    assert any("bracketed citation" in s for s in report.soft)


def test_hashtags_capped_citations_inlined_italics_removed():
    from app.writer import clean_post

    out = clean_post(
        "76% rely on workarounds (Smartsheet, 2025). 63% lose revenue (Zapier/Centiment, March 2026).\n\n"
        "A lack of *specific* software.\n\n* bullet stays\n\nWhat now?\n\n#Ops #InternalTools #StartupLife #PM #Students"
    )
    assert "workarounds, per Smartsheet." in out and "revenue, per Zapier/Centiment." in out
    assert "A lack of specific software." in out and "* bullet stays" in out
    assert out.endswith("What now?\n\n#Ops #InternalTools")  # capped at 2


def test_inline_hashtags_and_plain_brackets_untouched():
    from app.writer import clean_post

    text = "We use #buildinpublic daily (mostly).\n\nThe 2025 plan (v2) shipped.\n\nWhy?"
    assert clean_post(text) == text


async def test_fit_shape_retries_a_failed_shortening_once():
    long = "\n\n".join(["A line that says something useful here."] * 50)
    replies = iter(["99% agree.\n\nShort.", "\n\n".join(["A line that says something useful here."] * 25)])
    w = ScriptedWriter({"short": lambda t: next(replies)})
    out = await fit_shape(w, long, corpus="", hooks=[])
    assert w.modes == ["short", "short"] and len(out) < 1100 and "99%" not in out


# ── step 7d: lint → one targeted fix ────────────────────────────────────────
class FixWriter:
    def __init__(self, reply):
        self.reply = reply
        self.issues = None

    async def fix(self, text, issues):
        self.issues = issues
        return self.reply(text)


async def test_polish_fixes_banned_pattern_and_stacked_stats():
    from app.pipeline import polish

    text = "The real problem isn't a missing feature. It's fragmented context.\n\n93% said x. 63% said y. 76% said z.\n\nWhat now?"
    w = FixWriter(lambda t: "The real problem was fragmented context.\n\n93% said x. 63% said y.\n\nWhat now?")
    out = await polish(w, text, corpus="93% 63% 76%")
    assert out.startswith("The real problem was fragmented context.")
    assert any("it's not X, it's Y" in i for i in w.issues) and any("3 outside statistics" in i for i in w.issues)


async def test_polish_rejects_fixes_that_add_numbers_or_gut_the_post():
    from app.pipeline import polish

    text = "It's not about speed, it's about focus.\n\nWe shipped the tool in a day and it held.\n\nWhat now?"
    assert await polish(FixWriter(lambda t: "Focus wins. 88% agree.\n\nWe shipped the tool in a day and it held.\n\nWhat now?"), text, "") == text
    assert await polish(FixWriter(lambda t: "Focus."), text, "") == text


async def test_polish_skips_clean_drafts_without_a_model_call():
    from app.pipeline import polish

    w = FixWriter(lambda t: "changed")
    assert await polish(w, "Clean hook.\n\nOne line with 47% in it.\n\nWhat now?", "") == "Clean hook.\n\nOne line with 47% in it.\n\nWhat now?"
    assert w.issues is None


def test_emoji_lists_become_dashes_and_decorative_emoji_go():
    from app.writer import clean_post

    out = clean_post("🚀 Shipped it.\n\nWhat I did:\n🔹 Spot the task\n✅ Build it small\n👉 Let ops own it\n\nWhat now? 🏁")
    assert out.startswith("Shipped it.")  # emoji openers read as generated
    assert "- Spot the task\n- Build it small\n- Let ops own it" in out and out.endswith("What now?")
    assert not check_draft(out).hard


def test_draft_prompt_puts_the_per_call_part_last_for_prompt_caching():
    from app.writer import load_prompt

    system = load_prompt("draft_system")
    assert system.rstrip().endswith("This draft's role: {role}.")  # A and B share the long prefix
    tune = load_prompt("tune")
    assert tune.index("{copy_playbook}") < tune.index("{instruction}") < tune.index("{draft}")


# ── claim audit (part of step 7d) ────────────────────────────────────────────
class AuditWriter(FixWriter):
    def __init__(self, reply, audits):
        super().__init__(reply)
        self.audits = list(audits)

    async def audit_claims(self, post, brief, facts):
        return self.audits.pop(0)


BRIEF = "My bot copied an article's opening. I built a copy check that rewrites drafts overlapping more than 20%."
STORY = "I built a copy check.\n\nLinkedIn penalizes drafts that match 20% of their 8-word runs.\n\nWhat would you check?"


async def test_unsupported_claims_are_fixed():
    from app.pipeline import polish

    w = AuditWriter(lambda t: "I built a copy check.\n\nMy threshold: rewrite anything overlapping more than 20%.\n\nWhat would you check?",
                    [["LinkedIn penalizes drafts that match 20% of their 8-word runs."], []])
    out = await polish(w, STORY, corpus=BRIEF, brief=BRIEF, facts=["He studies CSE at LPU"])
    assert "My threshold" in out and "LinkedIn penalizes" not in out
    assert any("LinkedIn penalizes" in i for i in w.issues)


async def test_fix_that_leaves_the_false_claim_is_rejected():
    from app.pipeline import polish

    flagged = ["LinkedIn penalizes drafts that match 20% of their 8-word runs"]  # quoted without the full stop
    same = AuditWriter(lambda t: t.replace("What would", "What will"), [flagged])
    assert await polish(same, STORY, corpus=BRIEF, brief=BRIEF, facts=[]) == STORY


async def test_brief_numbers_are_his_data_not_outside_stats():
    from app.style_check import stat_count

    text = "Copied draft: 92% overlap. Normal: 1%. Threshold 20%. Plus 40% of feeds and 61% of teams."
    assert stat_count(text, own="92% ... 1% ... 20%") == 2 and stat_count(text) == 5


async def test_failing_audit_never_blocks_delivery():
    from app.pipeline import polish

    class Broken(FixWriter):
        async def audit_claims(self, post, brief, facts):
            raise RuntimeError("model down")

    clean = "Clean hook.\n\nOne line.\n\nWhat now?"
    assert await polish(Broken(lambda t: "x"), clean, "", brief="b", facts=[]) == clean


def test_fraction_and_percent_are_the_same_number():
    from app.verify import unverified_numbers

    assert unverified_numbers("The copied draft scored 92%.", "The copied draft scored 0.92") == []
    assert unverified_numbers("Overlap was 0.2 of the text.", "rewrites anything over 20%") == []
    assert unverified_numbers("It scored 93%.", "It scored 0.92") == ["93%"]


def test_claim_audit_targets_facts_about_him_not_opinions():
    from app.writer import load_prompt

    prompt = load_prompt("claim_audit")
    assert "outside findings presented as his experience" in prompt
    assert "Do NOT list: opinions, lessons, advice" in prompt and "When unsure, don't list it." in prompt


# ── story mode + ⚠ check lines ───────────────────────────────────────────────
def test_first_person_briefs_are_his_own_story():
    from app.pipeline import brief_hook, is_own_story

    assert is_own_story("My bot copied an article's opening")
    assert is_own_story("I built a copy check")
    assert not is_own_story("why student founders quit in year one")
    assert brief_hook("My LinkedIn bot copied a LangChain article's opening word for word. So I built a check.") == (
        "My LinkedIn bot copied a LangChain article's opening word for word."
    )
    assert len(brief_hook("word " * 60)) <= 140


async def test_own_story_gets_at_most_two_outside_facts(svc, fakes):
    from app.pipeline import generate

    gen = await generate(svc, "My bot copied an article's opening, so I built a copy check")
    assert gen.research["mode"] == "story" and len(gen.research["results"]) == 2


async def test_flags_the_audit_keeps_are_shown_under_the_draft(svc, fakes):
    from app.pipeline import run_brief

    fakes["nvidia"].unsupported = ["My post was flagged by LinkedIn."]
    post = await run_brief(svc, 42, "student founders quitting")
    assert post.research["claim_flags"]["a"] == ["My post was flagged by LinkedIn."]
    draft_a = next(t for t in fakes["messenger"].texts() if t.startswith("YOUR POST"))
    assert "⚠ check: “My post was flagged by LinkedIn.” — not in your brief or background" in draft_a


def test_his_own_edits_carry_no_flags():
    from app.db import Post
    from app.pipeline import claim_flags_for

    post = Post(id="00000000-0000-0000-0000-000000000001", chat_id=1, brief="b", status="awaiting_choice",
                edited_a=True, research={"claim_flags": {"a": ["x"], "b": ["y"]}})
    assert claim_flags_for(post) == {"a": [], "b": ["y"]}


async def test_tune_refreshes_the_flags_for_that_draft(h, fakes):
    post = fakes["db"].add_post(research={"results": [], "outline": {"hooks": ["h"]}, "claim_flags": {"a": ["old flag"], "b": ["keep"]}})
    fakes["nvidia"].unsupported = ["I launched it in May."]
    await tap(h, "tune", post.id, "a-bold")
    await drain(h)
    flags = fakes["db"].posts[post.id]["research"]["claim_flags"]
    assert flags == {"a": ["I launched it in May."], "b": ["keep"]}
    assert "⚠ check: “I launched it in May.”" in fakes["messenger"].texts()[-1]


def test_own_story_drafts_must_carry_his_numbers():
    from app.style_check import fixable_issues

    brief = "The copied draft scored 0.92; my normal drafts score 0.01. Threshold 20%."
    vague = "The copied draft scored very high; normal drafts are near zero."
    issues = fixable_issues(vague, brief, keep_numbers=True)
    assert any("0.92, 0.01, 20%" in i for i in issues)
    assert not fixable_issues("It scored 92% vs 0.01, over the 20% line.", brief, keep_numbers=True)  # 92% = 0.92
    assert not fixable_issues(vague, brief)  # not a story brief: no such rule


def test_own_story_leads_with_his_first_sentence():
    from app.pipeline import with_own_hook

    brief = "My LinkedIn bot copied a LangChain article's opening word for word. So I built a check."
    out = with_own_hook({"hooks": ["Your AI draft is copy-matched?", "h2", "h3"]}, brief)
    assert out["hooks"] == ["My LinkedIn bot copied a LangChain article's opening word for word.", "Your AI draft is copy-matched?", "h2"]
    assert with_own_hook({"hooks": ["h"]}, "why founders quit")["hooks"] == ["h"]


def test_repair_and_audit_prompts_close_the_gaps_seen_live():
    from app.writer import load_prompt

    assert "Never leave a sentence with a hole" in load_prompt("number_repair")
    audit = load_prompt("claim_audit")
    assert "how his tools or code work" in audit and "outcomes after the events in BRIEF" in audit


async def test_one_post_row_cannot_approve_an_empty_draft_b(h, fakes):
    post = fakes["db"].add_post(draft_b="")
    await tap(h, "pick", post.id, "b")
    assert fakes["db"].posts[post.id].get("chosen") is None and fakes["messenger"].answers()[-1] == "Expired"
    await tap(h, "pick", post.id, "a")
    assert fakes["db"].posts[post.id]["chosen"] == "a"
    assert fakes["messenger"].texts()[-1].startswith("✅ Approved. When should it go out?")


async def test_one_post_queue_confirmation_has_no_draft_letter(h, fakes):
    post = fakes["db"].add_post(draft_b="", chosen="a")
    await tap(h, "time", post.id, "6pm")
    assert fakes["messenger"].texts()[-1].startswith("Queued ✓ → ")


def test_sourced_statistics_are_not_flagged_but_misstatements_are():
    from app.pipeline import sourced_statistic

    sources = "Cloud Security Alliance survey: 53% of enterprises restrict autonomous AI to low-risk tasks."
    assert sourced_statistic("The CSA's April 2026 survey found that 53% of enterprises restrict agents.", sources.replace("53%", "53% (April 2026)"))
    assert not sourced_statistic("LinkedIn penalizes drafts that match 20% of their 8-word runs.", sources)  # 20% isn't in the research
    assert not sourced_statistic("At PicaPool I built an agent that hit 53% accuracy.", sources)  # about him: always checked
    assert not sourced_statistic("LinkedIn changed its feed.", sources)  # no number to verify


async def test_news_context_needs_the_topic_in_the_headline_or_summary():
    import httpx

    from app.news import NewsData

    arts = [
        {"title": "Romania's environmental tax review", "description": "Tax framework for buildings.", "link": "https://oecd/1", "source_id": "oecd", "source_priority": 1},
        {"title": "Why agent approval gates matter", "description": "Irreversible actions need human approval.", "link": "https://good/1", "source_id": "good", "source_priority": 50},
    ]
    news = NewsData(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "success", "results": arts}))), "k")
    picked = await news.context("Why AI agents need a human approval step before anything irreversible")
    assert [p["url"] for p in picked] == ["https://good/1"]


def test_flags_keep_claims_about_him_and_drop_advice():
    from app.pipeline import could_be_about_him

    facts = ["Strategic intern in PicaPool's Founder's Office since Jul 2025", "Gapl: AI resume intelligence"]
    assert could_be_about_him("When it tried to delete a message, I added a gate.", facts, "")
    assert could_be_about_him("PicaPool's support tickets spiked after the deletion.", facts, "")
    assert could_be_about_him("LinkedIn penalizes drafts that match 20% of their runs.", facts, "research without that number")
    assert not could_be_about_him("Tag every potential irreversible action before shipping.", facts, "")


def test_writer_gets_the_card_not_the_raw_list():
    from app.pipeline import writing_voice

    voice = {"current_role": "x", "background": ["PicaPool: 15+ tools"], "profile": "card"}
    assert writing_voice(voice, "Why AI agents need approval gates") == {"current_role": "x", "profile": "card"}
    assert writing_voice(voice, "My bot copied an article") == {"current_role": "x", "profile": "card"}
    no_card = {"current_role": "x", "background": ["PicaPool: 15+ tools"]}
    assert "background" not in writing_voice(no_card, "Why agents need gates")  # general topic, no card yet
    assert writing_voice(no_card, "My bot copied an article")["background"] == ["PicaPool: 15+ tools"]


async def test_writer_sees_his_profile_card_and_audits_keep_the_raw_facts(svc, fakes):
    from app.pipeline import run_brief

    fakes["db"].voice = {
        "current_role": "x",
        "background": ["RAWFACT: 15+ internal tools in 8 weeks"],
        "profile": "WHO: Deepanshu. WHAT HE DOES NOW: builds ProofMart (in development).",
    }
    post = await run_brief(svc, 42, "why agents need approval gates")
    assert post.research["background"] == ["RAWFACT: 15+ internal tools in 8 weeks"]  # verification + audits
    system = [c["messages"][0]["content"] for c in fakes["nvidia"].chat_calls if c["messages"][0]["role"] == "system"]
    drafts = [p for p in system if "This draft's role" in p]
    assert drafts and all("WHO: Deepanshu" in p and "RAWFACT" not in p for p in drafts)
    assert drafts[0].count("WHO: Deepanshu") == 1  # the card once, not again inside the voice JSON
    assert "WHO: Deepanshu" in fakes["nvidia"].final_prompts[0]
    assert "RAWFACT" in fakes["nvidia"].audit_prompts[0]

# ── human-sounding text ──────────────────────────────────────────────────────
def test_no_em_dashes_or_machine_typography_survive():
    from app.writer import clean_post

    out = clean_post("Autonomy is the goal — or so they say.\n\nPilots ran 2025–2026 on AI‑written drafts.\n\nWhat would you gate? 🔹\n\n#AI #Ops 🏁")
    assert "—" not in out and "–" not in out and "‑" not in out and " " not in out
    assert out.startswith("Autonomy is the goal, or so they say.")
    assert "2025-2026" in out and "AI-written" in out
    assert out.endswith("What would you gate?\n\n#AI #Ops")


def test_his_own_habits_are_kept():
    from app.writer import clean_post

    text = "Three steps:\n\n→ Tag risky calls\n→ Gate them\n\nP.S. What would you gate?"
    assert clean_post(text) == text


@pytest.mark.parametrize(
    "line",
    ["Here's the kicker: nobody checked.", "The result? Fewer mistakes.", "Lesson: ship ugly.", "Bottom line: gate it.",
     "Let's dive in.", "In a world where agents act alone, you need gates.", "A seamless, robust rollout.",
     "Moreover, it worked.", "This will revolutionize ops."],
)
def test_stock_ai_phrases_are_flagged_for_the_fix_step(line):
    assert any(i.startswith("Rewrite this line plainly") for i in __import__("app.style_check", fromlist=["x"]).fixable_issues(line))


def test_playbook_sets_light_sarcasm_and_no_dashes():
    from app.writer import load_prompt

    playbook = load_prompt("copy_playbook")
    assert "sarcasm, level 1 or 2 out of 10" in playbook and "no em dashes" in playbook
    import pathlib

    assert not any("—" in p.read_text() for p in pathlib.Path("app/prompts").glob("*.txt"))  # prompts model the style


async def test_auditor_sees_his_claim_rules(svc, fakes):
    from app.pipeline import run_brief

    fakes["db"].voice = {"current_role": "x", "background": ["Built an attribution system at PicaPool"],
                         "claim_rules": ["PicaPool internal systems: name them and describe the problem class only."]}
    await run_brief(svc, 42, "why dashboards count downloads")
    assert all("CLAIM RULE: PicaPool internal systems" in p for p in fakes["nvidia"].audit_prompts)


# ── invented details about his work ─────────────────────────────────────────
FACTS_PICAPOOL = ("Strategic intern in PicaPool's Founder's Office since Jul 2025; ops ran manually over a 79-table Supabase schema; "
                  "he built an event-fingerprinting attribution system; automated daily KPI digests replaced manual reporting.")
POST_EMBELLISHED = (
    "In July 2025 I walked into PicaPool's Founder's Office.\n\n"
    "No UTM columns, no event logs.\n\n"
    "I built an event-fingerprinting system:\n\n"
    "→ capture every touchpoint as a unique fingerprint (UTM + device + timestamp)\n"
    "→ auto-generate channel-level KPIs\n\n"
    "Within two weeks the dashboard showed channel counts.\n\n"
    "Counting downloads tells you nothing about the funnel."
)


def test_detector_flags_embellished_lines_about_his_work():
    from app.style_check import invented_details

    found = dict(invented_details(POST_EMBELLISHED, FACTS_PICAPOOL, {"PicaPool"}))
    assert found["No UTM columns, no event logs."] == ["columns", "utm"]
    assert set(found["→ capture every touchpoint as a unique fingerprint (UTM + device + timestamp)"]) == {"device", "timestamp", "utm"}
    assert found["Within two weeks the dashboard showed channel counts."] == ["two weeks"]
    assert "→ auto-generate channel-level KPIs" not in found  # KPI is in his facts; fingerprint(ing) too
    assert "Counting downloads tells you nothing about the funnel." not in found  # an opinion, no new details


def test_research_terms_are_fine_outside_his_story():
    from app.style_check import invented_details

    post = "Branch's survey found teams track UTM parameters on every channel.\n\nWhat do you track?"
    assert invented_details(post, FACTS_PICAPOOL, {"PicaPool"}, research="teams track UTM parameters on every channel") == []
    assert invented_details("At PicaPool we tracked UTM parameters.", FACTS_PICAPOOL, {"PicaPool"}, research="UTM parameters") != []


async def test_polish_removes_invented_details_and_flags_leftovers():
    from app.pipeline import polish

    class W:
        async def audit_claims(self, post, brief, facts):
            return []

        async def fix(self, text, issues):
            self.issues = issues
            return text.replace("No UTM columns, no event logs.", "Nobody could say which channel worked.")

    w = W()
    out = await polish(w, POST_EMBELLISHED, corpus="2025 79", brief="dashboards count downloads", facts=[FACTS_PICAPOOL], rounds=1)
    assert any("utm" in i.lower() for i in w.issues) and "No UTM columns" not in out


def test_elaboration_without_technical_words_is_caught():
    from app.style_check import invented_details

    post = ("What we did at PicaPool:\n\n"
            "→ Ran lift tests to isolate each channel's contribution.\n"
            "→ Tracked touchpoints across on-ground and digital funnels.\n")
    found = dict(invented_details(post, FACTS_PICAPOOL + " attribution across on-ground and digital funnels", {"PicaPool"}))
    assert "→ Ran lift tests to isolate each channel's contribution." in found
    assert "→ Tracked touchpoints across on-ground and digital funnels." not in found  # a faithful paraphrase


def test_bracket_citation_without_comma_is_inlined():
    from app.writer import clean_post

    assert clean_post("Most conversions are baseline (Measured.com 2026).") == "Most conversions are baseline, per Measured.com."


# ── status board + button behaviour ─────────────────────────────────────────
async def test_status_says_plainly_that_nothing_is_posted_and_the_publisher_is_off(h, fakes):
    fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW - timedelta(minutes=5), draft_a="Your dashboard screams downloads.\n\nBody", draft_b="")
    await h.on_text(CHAT_ID, "📋 Status")
    text, kb = fakes["messenger"].sent[-1][2]
    assert "Publisher: ❌ NOT RUNNING" in text
    assert "Your dashboard screams downloads." in text and "due now, waiting for the publisher" in text
    assert "nothing posted to LinkedIn yet" in text
    assert kb[0][0] == ("🔄 Refresh", "status:refresh")


async def test_status_with_a_running_publisher_and_a_posted_link(h, fakes):
    await fakes["db"].set_setting("dispatcher_last_run", (NOW - timedelta(minutes=2)).isoformat())
    await fakes["db"].set_setting("linkedin_token_issued_at", (NOW - timedelta(days=1)).isoformat())
    fakes["db"].add_post(status="posted", posted_at=NOW - timedelta(hours=1), post_url="https://www.linkedin.com/feed/update/urn:li:share:1")
    text = await h.status_text(CHAT_ID)
    assert "Publisher: ✅ running (last check 2 min ago)" in text and "LinkedIn: ✅ connected (59 days left" in text
    assert "https://www.linkedin.com/feed/update/urn:li:share:1" in text and "✅ Posted (1)" in text


async def test_menu_shortcut_buttons_run_commands(h, fakes):
    await h.on_callback(CHAT_ID, "cb", "menu:/queue")
    assert fakes["messenger"].texts()[-1] == "Nothing scheduled."
    await h.on_callback(CHAT_ID, "cb", "menu:/rm -rf")  # only known commands
    assert fakes["messenger"].answers()[-1] == "Expired"


@pytest.mark.parametrize(("arg", "delta"), [("30m", timedelta(minutes=30))])
async def test_new_time_options(h, fakes, arg, delta):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, arg)
    assert fakes["db"].posts[post.id]["scheduled_at"] == NOW + delta


async def test_tonight_9pm_option(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await tap(h, "time", post.id, "9pm")
    assert fakes["db"].posts[post.id]["scheduled_at"].astimezone(IST).hour == 21


async def test_a_real_tap_clears_spent_buttons(h, fakes):
    post = fakes["db"].add_post(chosen="a")
    await h.on_callback(CHAT_ID, "cb", build_callback("time", post.id, "6pm"), message_id=77)
    assert ("edit_keyboard", CHAT_ID, (77, None)) in fakes["messenger"].sent


async def test_a_dead_button_explains_itself_and_shows_status(h, fakes):
    post = fakes["db"].add_post(status="posted", posted_at=NOW)
    await h.on_callback(CHAT_ID, "cb", build_callback("pick", post.id, "a"), message_id=78)
    texts = fakes["messenger"].texts()
    assert texts[-2].startswith("That button is out of date") and texts[-1].startswith("📋 Status")
    assert ("edit_keyboard", CHAT_ID, (78, None)) in fakes["messenger"].sent


async def test_dispatcher_records_a_heartbeat(fakes):
    from datetime import UTC, datetime

    from dispatcher import HEARTBEAT_KEY, Dispatcher
    from tests.fakes import FakeMessenger

    class NoEmbed:
        async def embed_passage(self, text):
            return [0.0]

    d = Dispatcher(fakes["db"], NoEmbed(), FakeMessenger(), CHAT_ID, None, lambda url: None)
    now = datetime(2026, 9, 23, 16, 50, tzinfo=UTC)
    await d.run(now)
    assert fakes["db"].settings[HEARTBEAT_KEY] == now.isoformat()


# ── rules taken from sergebulaev/linkedin-skills ────────────────────────────
@pytest.mark.parametrize(
    "line",
    ["Here's what nobody tells you about attribution.", "Stop counting downloads, start counting channels.",
     "Let me be honest: it hurt.", "When it comes to growth, measure.", "The hard truth is nobody checked.",
     "This will move the needle.", "I'm excited to share our launch.", "What do you think?", "Agree or disagree?",
     "Short. Punchy. Done.", "No meetings. No decks. Just code.", "Why? Because nobody measured."],
)
def test_repo_single_hit_tells_are_caught(line):
    from app.style_check import fixable_issues

    assert any(i.startswith("Rewrite this line plainly") for i in fixable_issues(line))


def test_vocabulary_is_scored_per_paragraph_not_per_word():
    from app.style_check import fixable_issues

    one = "We found notably better results."  # one marker is English
    three = "We leveraged comprehensive insights to foster growth."
    assert not any("machine-written" in i for i in fixable_issues(one))
    assert any("machine-written" in i for i in fixable_issues(three))


def test_fragment_limit_follows_his_voice():
    from app.style_check import FRAGMENT_LIMIT, fixable_issues

    his_normal = "Wrong. Again. We shipped it anyway and it held up fine for months.\n\nNot bad. Next."
    assert FRAGMENT_LIMIT == 4 and not any("one-to-three-word" in i for i in fixable_issues(his_normal))
    assert any("one-to-three-word" in i for i in fixable_issues("Yes. No. Maybe. Fine. Done. Ship."))


def test_curly_double_quotes_are_straightened():
    from app.writer import clean_post

    assert clean_post("He said “ship it” and left.") == 'He said "ship it" and left.'


def test_links_never_stay_in_the_post_body():
    from app.writer import clean_post

    out = clean_post("Most conversions are baseline (https://measured.com/report).\n\nRead more: https://x.com/a/status/1\n\nWhy?")
    assert "http" not in out and out.startswith("Most conversions are baseline.")


async def test_crowded_schedule_gets_a_heads_up_and_a_time_hint(h, fakes):
    fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(hours=5), draft_b="")
    post = fakes["db"].add_post(draft_b="")
    await tap(h, "pick", post.id, "a")
    assert "Best reach: Tue-Thu" in fakes["messenger"].texts()[-1]
    await tap(h, "time", post.id, "6pm")
    assert fakes["messenger"].texts()[-1].startswith("Heads-up: another post goes out")


async def test_no_heads_up_when_posts_are_spread_out(h, fakes):
    fakes["db"].add_post(status="queued", chosen="a", scheduled_at=NOW + timedelta(days=3), draft_b="")
    post = fakes["db"].add_post(chosen="a", draft_b="")
    await tap(h, "time", post.id, "6pm")
    assert not fakes["messenger"].texts()[-1].startswith("Heads-up")
