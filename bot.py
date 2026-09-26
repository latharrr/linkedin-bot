"""Telegram bot (SPEC §5). One update handler; order: whitelist → callbacks →
chat state → commands → post requests → chat last. Plain messages are a
conversation; "make a post about X", "draft: X" or a pasted link start drafts.

    python bot.py                 long-polling bot (run under systemd)
    python bot.py --nudge         8:00 IST  "What's today's topic?"
    python bot.py --fallback      14:00 IST drafts from news if no brief today
    python bot.py --token-check   weekly    warn if LinkedIn token > 50 days old

Nothing here posts to LinkedIn. Posting happens only in dispatcher.py, and only
for rows a human moved to 'queued' by picking a draft AND a time.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from telegram import Bot, BotCommand, Update
from telegram.ext import Application, ContextTypes, TypeHandler

from app import corpus, storybank
from app.chat import (
    MIN_TOPIC_CHARS,
    ChatMemory,
    DraftRequest,
    is_cancel_request,
    is_decline,
    is_status_query,
    parse_draft_request,
    wants_bot_to_pick,
)
from app.config import Settings, get_settings
from app.db import ChatState, Post
from app.jev import THRESHOLD as JEV_THRESHOLD
from app.linkedin import escape_little_text, looks_like_post_url
from app.linkread import find_urls
from app.log import get_logger, setup_logging
from app.pipeline import (
    audit_facts,
    audit_flags,
    copied_for,
    make_images,
    preview,
    regenerate,
    run_brief,
    upload_images,
)
from app.research import research_corpus
from app.services import Services, build_services
from app.style_check import airy
from app.telegram_ui import (
    Callback,
    Keyboard,
    Messenger,
    TelegramMessenger,
    draft_keyboard,
    guard_allows,
    parse_callback,
    queued_keyboard,
    send_draft,
    telegram_request,
    time_keyboard,
    waiting_keyboard,
)
from app.timeutil import (
    IST,
    custom_time,
    format_ist,
    ist_day_start,
    next_6pm,
    next_9am,
    next_ist_occurrence,
    parse_ts,
    parse_when,
    utcnow,
)
from app.verify import unverified_numbers

log = get_logger("bot")

MAX_BRIEF_CHARS = 800  # longer text is almost certainly a draft pasted after an edit session expired
MIN_EDIT_CHARS = 20
LINKEDIN_MAX_CHARS = 3000
TOKEN_WARN_DAYS = 50
NOW_DELAY = timedelta(minutes=2)

HELP = (
    "Talk to me normally. When you want a post, say so:\n"
    "• make a post about <topic>   • draft: <topic>\n"
    "• paste a link + \"something on this?\"\n"
    "• make a post — I'll ask what about (\"you pick\" = today's news)\n"
    "Or just ask: \"what's queued\", \"any drafts waiting\", \"how's usage looking\", \"what should I post about\".\n"
    "Or tell me: \"schedule it for 6pm\", \"move it to Friday 9am\", \"unschedule it\", \"make it shorter\" (I'll ask ✅ first).\n\n"
    "I research it with every source (Groq browsing, Bright Data, Tavily, NewsData), write\n"
    "two versions behind the scenes, and send you ONE final post with one image. Under it:\n"
    "✂️ Shorter · 🎣 New hook · 🔥 Bolder · ✅ Post this · ✏️ Edit\n"
    "Then: 🔄 New version · 🖼 New image · 🗑 Discard\n"
    "Nothing posts until you tap ✅ Post this AND pick a time.\n\n"
    "/status — is it posted? what's scheduled? is the publisher on?\n"
    "/new <topic or link> — draft a post\n"
    "/interview — answer a few questions so posts use your true stories\n"
    "/comment <post link or text> — two comment drafts to copy\n"
    "/ideas — 3 fresh topic ideas from the news\n"
    "/drafts — drafts waiting for a pick\n"
    "/queue — scheduled posts (post now / unschedule)\n"
    "/dashboard — API usage: used vs left\n"
    "/stats — log likes & comments for recent posts\n"
    "/cancel — cancel a pending edit / time / stats / URL reply"
)
MENU = [["✍️ New post", "📋 Status"], ["📝 Drafts", "📅 Queue"], ["💬 Comment", "🎙 Interview"], ["💡 Ideas", "📊 Dashboard", "❓ Help"]]
MENU_COMMANDS = {
    "✍️ New post": "/new", "📋 Status": "/status", "💡 Ideas": "/ideas", "📝 Drafts": "/drafts",
    "📅 Queue": "/queue", "📊 Dashboard": "/dashboard", "❓ Help": "/help", "🎙 Interview": "/interview", "💬 Comment": "/comment",
}
PUBLISHER_STALE = timedelta(minutes=10)  # the dispatcher runs every 5 min when live
CROWDED = timedelta(hours=24)
TIME_HINT = "Best reach: Tue-Thu, 7:30-9 AM. Weekends and Friday afternoons are slow."
STALE_AFTER = {"time", "unq", "asap", "drop"}  # after these, the tapped message's buttons are spent
DEAD_ANSWERS = {"Expired", "Too late — already posting"}
# Shown in Telegram's "/" menu (setMyCommands at startup).
BOT_COMMANDS = [
    ("status", "Is it posted? Scheduled, posted and publisher state"),
    ("new", "Draft a post: /new <topic or link>"),
    ("comment", "Draft a comment: /comment <LinkedIn post link or text>"),
    ("interview", "Answer a few questions: true stories for your posts"),
    ("bank", "What your story bank holds"),
    ("ideas", "3 fresh topic ideas from the news"),
    ("drafts", "Drafts waiting for a pick"),
    ("queue", "Scheduled posts"),
    ("dashboard", "API usage: used vs left"),
    ("stats", "Log likes & comments"),
    ("help", "What I can do"),
    ("cancel", "Cancel a pending reply"),
]
# Answered even while a reply is pending (e.g. mid-edit): they read, they never write drafts.
QUICK_COMMANDS = ("/start", "/help", "/new", "/status", "/ideas", "/drafts", "/queue", "/dashboard", "/interview", "/bank", "/comment")
IDEAS = 3
# Chat actions (step 3): which post statuses each can act on. CONFIRMED ones touch the
# publish path or throw work away, so the model only proposes them and he taps ✅.
ACTION_STATUSES: dict[str, tuple[str, ...]] = {
    "schedule": ("awaiting_choice",),
    "reschedule": ("queued",),
    "unschedule": ("queued",),
    "discard": ("awaiting_choice", "queued"),
    "tune": ("awaiting_choice",),
    "new_image": ("awaiting_choice",),
    "show": ("awaiting_choice",),
}
CONFIRMED = {"schedule", "reschedule", "unschedule", "discard"}
RELATIVE_TIMES = {"now": NOW_DELAY, "30m": timedelta(minutes=30)}
MAX_CHOICES = 5
REF_TAG = re.compile(r"[ \t]*[\[(]?\bref[:\s]+[0-9a-f]{6,}[\])]?", re.IGNORECASE)
WHEN_HINT = "When should it go out? e.g. 6pm, 18:30, or 2026-09-29 08:00 (IST)."
TUNE_LABELS = {"short": "✂️ Shortening", "hook": "🎣 Rewriting the hook of", "bold": "🔥 Sharpening"}
ASK_TOPIC = "What should the post be about? Send a topic or a link — or say \"you pick\" and I'll draft from today's news."
CHAT_DOWN = "I couldn't reach the chat model just now. To start drafts anyway, say: make a post about <topic>."
REAUTH_STEPS = (
    "LinkedIn re-auth (≈2 min):\n"
    "1. On your laptop: python scripts/linkedin_auth.py\n"
    "2. Sign in and approve in the browser tab it opens.\n"
    "3. It stores the new token, issue date and author URN in Supabase.\n"
    "Posting fails once the token expires (~60 days), so do this soon."
)


# ── pure helpers ─────────────────────────────────────────────────────────────
_STATS_LINE = re.compile(r"^\s*(\d+)\D+(\d+)\s*$")


def parse_stats_lines(text: str, expected: int) -> list[tuple[int, int] | None] | str:
    """One 'likes comments' line per post, '-' to skip. Returns rows or an error message."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != expected:
        return f"Expected {expected} lines (one per post), got {len(lines)}."
    rows: list[tuple[int, int] | None] = []
    for i, line in enumerate(lines, 1):
        if line.strip() in ("-", "skip"):
            rows.append(None)
            continue
        m = _STATS_LINE.match(line)
        if not m:
            return f"Line {i} should look like '120 14' (likes comments) or '-' to skip."
        rows.append((int(m.group(1)), int(m.group(2))))
    return rows


def queued_text(which: str | None, when: datetime, single: bool = False) -> str:
    return f"Queued ✓ → {format_ist(when)}" if single else f"Queued ✓ Draft {(which or '?').upper()} → {format_ist(when)}"


@dataclass
class ActiveRun:
    """One draft generation in flight for a chat. Tracked so a "?" or "cancel" a few
    minutes later is handled deterministically, instead of asking the chat model to
    guess — see app.chat.is_status_query / is_cancel_request."""

    brief: str
    started_at: datetime
    stage: str = "starting"
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


@dataclass
class PendingAction:
    """A chat-proposed change waiting for his ✅. One per chat: a newer proposal replaces
    it, so the older message's buttons answer "Expired"."""

    nonce: str
    name: str
    post_id: str
    when_spec: str | None = None
    when: datetime | None = None
    draft: str | None = None


def post_ref(post: Post) -> str:
    """Short id the chat model can hand back in an action (a uuid prefix)."""
    return post.id[:8]


# ── handler ──────────────────────────────────────────────────────────────────
class BotHandler:
    def __init__(self, svc: Services, now: Callable[[], datetime] = utcnow) -> None:
        assert svc.messenger is not None
        self.svc = svc
        self.db = svc.db
        self.msg: Messenger = svc.messenger
        self.now = now
        self.tasks: set[asyncio.Task[Any]] = set()
        self.regenerating: set[str] = set()
        self.memory = ChatMemory()
        self.ideas: dict[int, list[str]] = {}  # last /ideas per chat, for the ✍️ Draft buttons
        self.interviews: dict[int, dict[str, Any]] = {}  # chat → {"qid", "pressed", "skipped", "answered"}
        self.active_runs: dict[int, ActiveRun] = {}  # chat → the draft generating right now, if any
        self.pending: dict[int, PendingAction] = {}  # chat → the chat action awaiting ✅ / ✖
        self.choosing: dict[int, tuple[str, dict[str, Any], list[str]]] = {}  # chat → (nonce, action, post ids) awaiting a pick

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def say(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> None:
        await self.msg.send_text(chat_id, text, keyboard)

    # entry point ---------------------------------------------------------------
    async def on_update(self, update: Update) -> None:
        chat = update.effective_chat
        if chat is None or chat.id != self.svc.settings.my_chat_id:
            return  # whitelist first: strangers can't trigger paid runs
        try:
            if update.callback_query:
                cq = update.callback_query
                message_id = cq.message.message_id if cq.message is not None else None
                await self.on_callback(chat.id, cq.id, cq.data, message_id)
            elif update.message and update.message.text is not None:
                await self.on_text(chat.id, update.message.text)
        except Exception:
            log.exception("update_failed")
            await self.say(chat.id, "Something went wrong handling that. Check the bot logs.")

    # text ------------------------------------------------------------------------
    async def on_text(self, chat_id: int, raw: str) -> None:
        text = raw.strip()
        run = self.active_runs.get(chat_id)
        if text.lower() == "/cancel":
            await self.db.clear_chat_state(chat_id)
            if run is not None:
                return await self.cancel_run(chat_id, run)
            return await self.say(chat_id, "Cancelled.")
        text = MENU_COMMANDS.get(text, text)  # a tap on the quick menu is a command
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower() if parts else ""  # "/queue@linkedinwala_bot" in groups
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in QUICK_COMMANDS:
            return await self.on_command(chat_id, cmd, arg.strip())
        state = await self.db.get_chat_state(chat_id, self.now())
        if state is not None:
            return await self.on_state_reply(chat_id, state, text)
        if run is not None:  # a draft is generating: a check-in or cancel never starts another one
            if is_cancel_request(text):
                return await self.cancel_run(chat_id, run)
            if is_status_query(text):
                return await self.say(chat_id, self.run_status_text(run))
        if text == "/stats":
            return await self.stats_flow(chat_id)
        request = parse_draft_request(text)
        if request is None and find_urls(text):
            request = DraftRequest(text)  # a pasted link is a request for a post on it
        if request is not None:
            return await self.on_draft_request(chat_id, text, request)
        if text.startswith("/"):
            return await self.say(chat_id, "Unknown command.\n\n" + HELP)
        await self.chat_flow(chat_id, text)

    # commands ----------------------------------------------------------------------
    async def on_command(self, chat_id: int, cmd: str, arg: str) -> None:
        if cmd in ("/start", "/help"):
            await self.say(chat_id, HELP)
            return await self.msg.send_menu(chat_id, "Quick menu is under the message box 👇", MENU)
        if cmd == "/new":
            if not arg:
                return await self.ask_topic(chat_id)
            return await self.on_draft_request(chat_id, f"/new {arg}", DraftRequest(arg))
        if cmd == "/ideas":
            return await self.ideas_flow(chat_id)
        if cmd == "/drafts":
            return await self.list_posts(chat_id, "awaiting_choice")
        if cmd == "/queue":
            return await self.list_posts(chat_id, "queued")
        if cmd == "/dashboard":
            return await self.dashboard_flow(chat_id)
        if cmd == "/status":
            return await self.status_flow(chat_id)
        if cmd == "/interview":
            return await self.start_interview(chat_id)
        if cmd == "/comment":
            if not arg:
                await self.db.set_chat_state(chat_id, "awaiting_comment", None, self.now())
                return await self.say(chat_id, "💬 Send the LinkedIn post link, or paste the post's text.")
            self.spawn(self.comment_flow(chat_id, arg))
            return None
        if cmd == "/bank":
            bank = storybank.load(await self.db.get_setting(storybank.SETTING_KEY))
            return await self.say(chat_id, storybank.summary(bank), [[("🎙 Continue the interview", "menu:/interview")]])

    async def list_posts(self, chat_id: int, status: str) -> None:
        posts = await self.db.posts_with_status(chat_id, status, 5)  # type: ignore[arg-type]
        if not posts:
            empty = "No drafts waiting. Send /new <topic>." if status == "awaiting_choice" else "Nothing scheduled."
            return await self.say(chat_id, empty)
        for post in posts:
            if status == "queued":
                first = (post.draft(post.chosen or "a").strip().splitlines() or [""])[0][:90]
                when = format_ist(post.scheduled_at) if post.scheduled_at else "?"
                await self.say(chat_id, f"📅 {when} — Draft {(post.chosen or '?').upper()}\n{first}", queued_keyboard(post.id))
            else:
                made = format_ist(post.created_at) if post.created_at else ""
                await self.say(chat_id, f"📝 {(post.topic or post.brief)[:140]}\n{made}", waiting_keyboard(post.id))

    async def posts_tool_text(self, chat_id: int, status: str) -> str:
        """Same data as /drafts or /queue, as one plain-text digest for the chat tool
        loop — no per-post buttons, since those only make sense in the direct command."""
        posts = await self.db.posts_with_status(chat_id, status, 5)  # type: ignore[arg-type]
        if not posts:
            return "No drafts waiting." if status == "awaiting_choice" else "Nothing scheduled."
        lines = []
        for post in posts:
            if status == "queued":
                first = (post.draft(post.chosen or "a").strip().splitlines() or [""])[0][:90]
                when = format_ist(post.scheduled_at) if post.scheduled_at else "?"
                lines.append(f"- [ref {post_ref(post)}] {when} — Draft {(post.chosen or '?').upper()}: {first}")
            else:
                made = format_ist(post.created_at) if post.created_at else ""
                lines.append(f"- [ref {post_ref(post)}] {(post.topic or post.brief)[:140]} ({made})")
        return "\n".join(lines)

    # comments on other people's posts (idea from sergebulaev/linkedin-skills) ---------------
    async def comment_flow(self, chat_id: int, source: str) -> None:
        """Two comment drafts to copy. The bot never posts comments."""
        urls = find_urls(source)
        post_text = source
        if urls:
            await self.say(chat_id, "💬 Reading the post…")
            page = await self.svc.links.read(urls[0]) if self.svc.links else None
            if not page:
                await self.db.set_chat_state(chat_id, "awaiting_comment", None, self.now())
                return await self.say(chat_id, "I couldn't read that post. Paste its text here instead.")
            post_text = page["content"]
        if len(post_text.split()) < 8:
            return await self.say(chat_id, "That's too short to comment on. Send the post link or paste its full text.")
        try:
            voice = await self.db.get_voice_profile() or {}
            bank = storybank.load(await self.db.get_setting(storybank.SETTING_KEY))
            text = storybank.as_text(bank)
            drafts = await self.svc.writer.comments(post_text, {**voice, "story_bank": text} if text else voice)
        except Exception as exc:
            log.exception("comment_failed")
            return await self.say(chat_id, f"Comment drafting failed: {type(exc).__name__}: {str(exc)[:200]}")
        await self.say(chat_id, "💬 Two options. Copy the one you like and post it yourself (I never post comments):")
        for i, d in enumerate(drafts, 1):
            await self.say(chat_id, f"{i}. {d['template']} · {len(d['text'])} chars\n\n{d['text']}")

    # story bank interview (idea from sergebulaev/linkedin-skills) -------------------------
    async def _bank(self) -> dict[str, Any]:
        return storybank.load(await self.db.get_setting(storybank.SETTING_KEY))

    async def start_interview(self, chat_id: int) -> None:
        bank = await self._bank()
        bank["sessions"] = bank.get("sessions", 0) + 1
        await self.db.set_setting(storybank.SETTING_KEY, storybank.dump(bank))
        self.interviews[chat_id] = {"qid": None, "pressed": False, "skipped": set(), "answered": 0}
        await self.say(
            chat_id,
            "🎙 A few questions, one at a time. Your answers go into your story bank, word for word, "
            "so posts can use true details instead of made-up ones. Answer however you like; "
            "\"skip\" moves on, \"rather not\" means I never ask that again, \"done\" stops.",
        )
        await self.ask_next(chat_id, bank)

    async def ask_next(self, chat_id: int, bank: dict[str, Any]) -> None:
        session = self.interviews.setdefault(chat_id, {"qid": None, "pressed": False, "skipped": set(), "answered": 0})
        q = storybank.next_question(bank, session["skipped"])
        if q is None:
            return await self.end_interview(chat_id, "That's every question for now.")
        session.update(qid=q.id, pressed=False)
        await self.db.set_chat_state(chat_id, "awaiting_interview", None, self.now())
        buttons = [[("⏭ Skip", "iv:skip"), ("🚫 Never ask", "iv:never")], [("✅ Done for now", "iv:done")]]
        await self.say(chat_id, f"🎙 {q.text}", buttons)

    async def interview_answer(self, chat_id: int, text: str) -> None:
        session = self.interviews.get(chat_id)
        q = storybank.BY_ID.get(session["qid"]) if session and session.get("qid") else None
        if q is None:  # a restart lost the session: pick up where the bank is thin
            await self.db.clear_chat_state(chat_id)
            return await self.start_interview(chat_id)
        kind = storybank.classify(text)
        if kind in ("stop", "never", "skip"):
            return await self.interview_button(chat_id, {"stop": "done", "never": "never", "skip": "skip"}[kind])
        bank = await self._bank()
        if session["pressed"]:
            bank = storybank.add_follow_up(bank, q, text, self.now())
        else:
            bank = storybank.add_answer(bank, q, text, self.now())
            session["answered"] += 1
        await self.db.set_setting(storybank.SETTING_KEY, storybank.dump(bank))
        if not session["pressed"] and storybank.is_vague(q, text):
            session["pressed"] = True  # press once, then move on
            await self.db.set_chat_state(chat_id, "awaiting_interview", None, self.now())
            return await self.say(chat_id, storybank.PRESS, [[("⏭ Skip", "iv:skip"), ("✅ Done for now", "iv:done")]])
        await self.ask_next(chat_id, bank)

    async def interview_button(self, chat_id: int, action: str) -> str:
        session = self.interviews.get(chat_id)
        q = storybank.BY_ID.get(session["qid"]) if session and session.get("qid") else None
        if action == "done" or q is None:
            await self.end_interview(chat_id, "Saved.")
            return "Saved"
        bank = await self._bank()
        if action == "never":
            bank = storybank.decline(bank, q)
            await self.db.set_setting(storybank.SETTING_KEY, storybank.dump(bank))
            await self.say(chat_id, "Got it, I won't ask that again.")
        else:
            session["skipped"].add(q.id)
        await self.ask_next(chat_id, bank)
        return "Next"

    async def end_interview(self, chat_id: int, lead: str) -> None:
        session = self.interviews.pop(chat_id, None) or {}
        await self.db.clear_chat_state(chat_id)
        bank = await self._bank()
        n = session.get("answered", 0)
        note = f"{lead} {n} new answer{'s' if n != 1 else ''} in your story bank. Posts will use them as true details."
        await self.say(chat_id, note + "\n\n" + storybank.summary(bank), [[("✍️ New post", "menu:/new"), ("🎙 More questions", "menu:/interview")]])

    async def status_text(self, chat_id: int) -> str:
        """Exactly what is posted, what is scheduled, and whether anything will publish it."""
        now = self.now()
        counts = await self.db.status_counts(chat_id)
        beat = parse_ts(await self.db.get_setting("dispatcher_last_run"))
        token_at = parse_ts(await self.db.get_setting("linkedin_token_issued_at"))
        lines = [f"📋 Status · {format_ist(now)}", ""]
        if beat is None:
            lines.append("Publisher: ❌ NOT RUNNING. Approved posts will not reach LinkedIn until it's started.")
        elif now - beat > PUBLISHER_STALE:
            lines.append(f"Publisher: ⚠️ last ran {format_ist(beat)}, so it looks stopped. Queued posts are waiting.")
        else:
            lines.append(f"Publisher: ✅ running (last check {int((now - beat).total_seconds() // 60)} min ago)")
        if token_at is None:
            lines.append("LinkedIn: ❌ not connected")
        else:
            left = 60 - (now - token_at).days
            lines.append(f"LinkedIn: ✅ connected ({left} days left on the login)")
        queued = await self.db.posts_with_status(chat_id, "queued", 5)
        lines += ["", f"⏰ Scheduled ({counts.get('queued', 0)})"]
        for p in queued:
            first = (p.draft(p.chosen or "a").strip().splitlines() or [""])[0][:70]
            when = p.scheduled_at
            due = when is not None and when <= now
            state = "due now, waiting for the publisher" if due and (beat is None or now - beat > PUBLISHER_STALE) else ("posting within 5 min" if due else "waiting")
            lines.append(f"• {format_ist(when) if when else '?'}: {first}\n   {state}")
        if not queued:
            lines.append("• nothing scheduled")
        posted = await self.db.last_posted(1)
        lines += ["", f"✅ Posted ({counts.get('posted', 0)})"]
        lines.append(f"• last: {format_ist(posted[0].posted_at)} {posted[0].post_url or ''}".rstrip() if posted else "• nothing posted to LinkedIn yet")
        if counts.get("posting"):
            lines.append(f"🔄 Publishing right now: {counts['posting']}")
        lines += ["", f"📝 Waiting for your approval: {counts.get('awaiting_choice', 0)}", f"🗑 Discarded or failed: {counts.get('failed', 0)}"]
        return "\n".join(lines)

    async def status_flow(self, chat_id: int) -> None:
        buttons = [[("🔄 Refresh", "status:refresh"), ("📅 Queue", "menu:/queue")], [("📝 Drafts", "menu:/drafts"), ("✍️ New post", "menu:/new")]]
        await self.say(chat_id, await self.status_text(chat_id), buttons)

    async def _fetch_ideas(self) -> list[str]:
        keywords = self.svc.settings.keywords
        if not keywords:
            return []
        day = self.now().astimezone(IST).toordinal()
        picks = [keywords[(day + i) % len(keywords)] for i in range(min(IDEAS, len(keywords)))]
        if self.svc.news is not None:
            found = await asyncio.gather(*(self.svc.news.topic(k) for k in picks), return_exceptions=True)
            return [t for t in found if isinstance(t, str)]
        return [t for t in [await self.svc.researcher.news_topic(keywords, day)] if t]  # one call only: costs research tokens

    async def ideas_flow(self, chat_id: int) -> None:
        """Three fresh headlines (one per niche keyword), each with a ✍️ Draft button."""
        if not self.svc.settings.keywords:
            return await self.say(chat_id, "Set NICHE_KEYWORDS in .env first, e.g. AI agents,startup,developer tools.")
        await self.say(chat_id, "💡 Finding fresh ideas…")
        topics = await self._fetch_ideas()
        if not topics:
            return await self.say(chat_id, "No fresh ideas right now. Send me a topic instead.")
        self.ideas[chat_id] = topics
        lines = [f"{i}. {t.split(' (niche:', 1)[0][:260]}" for i, t in enumerate(topics, 1)]
        buttons = [[(f"✍️ Draft {i}", f"idea:{i}") for i in range(1, len(topics) + 1)]]
        await self.say(chat_id, "💡 Ideas from today's news:\n\n" + "\n\n".join(lines), buttons)

    async def ideas_tool_text(self) -> str:
        if not self.svc.settings.keywords:
            return "No niche keywords configured (NICHE_KEYWORDS in .env)."
        topics = await self._fetch_ideas()
        if not topics:
            return "No fresh ideas right now."
        return "\n".join(f"{i}. {t.split(' (niche:', 1)[0][:260]}" for i, t in enumerate(topics, 1))

    async def _collect_usage(self) -> dict[str, Any]:
        from app.usage import collect

        if self.svc.http is not None:
            return await collect(self.svc.settings, self.db, self.svc.http)
        async with httpx.AsyncClient() as http:
            return await collect(self.svc.settings, self.db, http)

    async def dashboard_flow(self, chat_id: int) -> None:
        from app.usage import cards_text

        await self.say(chat_id, await self.status_text(chat_id))  # posts first: "is it posted?"
        await self.say(chat_id, "📊 Checking every API…")
        data = await self._collect_usage()
        for part in cards_text(data):
            await self.say(chat_id, part)

    async def dashboard_tool_text(self) -> str:
        from app.usage import cards_text

        return "\n\n".join(cards_text(await self._collect_usage()))

    # conversation ------------------------------------------------------------------
    async def on_draft_request(self, chat_id: int, text: str, request: DraftRequest) -> None:
        if request.topic:
            self.memory.add(chat_id, "user", text)
            return await self.start_generation(chat_id, request.topic, echo=request.topic != text)
        if self.memory.has_context(chat_id):  # "draft that": the chat model resolves "that"
            return await self.chat_flow(chat_id, text)
        await self.ask_topic(chat_id)

    async def ask_topic(self, chat_id: int) -> None:
        await self.db.set_chat_state(chat_id, "awaiting_brief", None, self.now())
        self.memory.add(chat_id, "assistant", ASK_TOPIC)
        await self.say(chat_id, ASK_TOPIC)

    async def save_brief(self, chat_id: int, text: str) -> None:
        """Reply to "what should the post be about?" (from a topic-less request or the 8am nudge)."""
        await self.db.clear_chat_state(chat_id)
        if is_decline(text):
            return await self.say(chat_id, "Okay — no post for now. Message me whenever you want one.")
        if wants_bot_to_pick(text):
            return await self.draft_from_news(chat_id)
        request = parse_draft_request(text)
        topic = request.topic if request and request.topic else text
        self.memory.add(chat_id, "user", text)
        await self.start_generation(chat_id, topic, echo=topic != text)

    async def draft_from_news(self, chat_id: int) -> None:
        try:
            topic = await self.svc.researcher.news_topic(self.svc.settings.keywords, self.now().astimezone(IST).toordinal())
        except Exception:
            log.exception("news_topic_failed")
            topic = None
        if not topic:
            return await self.say(chat_id, "I couldn't find a fresh news topic right now. Send me a topic instead.")
        await self.start_generation(chat_id, topic, echo=True)

    async def chat_flow(self, chat_id: int, text: str) -> None:
        if len(text) > MAX_BRIEF_CHARS:
            return await self.say(
                chat_id,
                f"That's over {MAX_BRIEF_CHARS} characters — it looks like a full post. "
                "If it's an edited draft, your edit window expired: tap Edit again, then resend.",
            )
        self.memory.add(chat_id, "user", text)
        try:
            voice = await self.db.get_voice_profile()
            run_status = self.run_status_for_model(self.active_runs.get(chat_id))
            tools = {
                "status": lambda: self.status_text(chat_id),
                "queue": lambda: self.posts_tool_text(chat_id, "queued"),
                "drafts": lambda: self.posts_tool_text(chat_id, "awaiting_choice"),
                "dashboard": self.dashboard_tool_text,
                "ideas": self.ideas_tool_text,
            }
            clock = format_ist(self.now())
            out = await self.svc.writer.converse(self.memory.history(chat_id), voice, run_status, tools, clock)
        except Exception as exc:
            log.warning("chat_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
            return await self.say(chat_id, CHAT_DOWN)
        if out.get("action"):
            return await self.chat_action(chat_id, out["action"])
        reply, topic = REF_TAG.sub("", out["reply"]), out["draft_topic"]  # refs are internal, never shown
        if topic and self.svc.jev is not None:
            p = await self.svc.jev.wants_drafts(self.memory.history(chat_id))
            log.info("jev_gate", extra={"p": p})
            if p is not None and p < JEV_THRESHOLD:  # Jev says this wasn't a request: offer instead
                offer = f"Want me to draft a post on: {topic}\nSay \"draft that\" and I'll start."
                self.memory.add(chat_id, "assistant", offer)
                return await self.say(chat_id, offer)
        if topic and len(topic) >= MIN_TOPIC_CHARS:
            self.memory.add(chat_id, "assistant", f"(started drafts on: {topic})")
            if reply:
                await self.say(chat_id, reply)
            return await self.start_generation(chat_id, topic, echo=True)
        self.memory.add(chat_id, "assistant", reply or "…")
        await self.say(chat_id, reply or "Say that again?")

    # chat actions (step 3) ---------------------------------------------------------
    async def chat_action(self, chat_id: int, action: dict[str, Any]) -> None:
        """Act on what the chat model proposed. The reply here is always written by the bot,
        never the model, so chat can't claim something happened that didn't."""
        name = str(action.get("name") or "")
        log.info("chat_action", extra={"action": name})
        statuses = ACTION_STATUSES.get(name)
        if statuses is None:
            return await self.tell(chat_id, "I can't do that from chat. Use the buttons under the post, or /help.")
        post = await self.resolve_post(chat_id, action.get("post"), statuses)
        if isinstance(post, str):
            return await self.tell(chat_id, post)
        if isinstance(post, list):
            return await self.ask_which(chat_id, action, post)
        draft = str(action.get("draft") or "").lower() or None
        if name in ("schedule", "tune") and draft not in ("a", "b"):
            draft = post.chosen or (None if post.draft_b else "a")
            if draft is None:
                return await self.tell(chat_id, "Which version, A or B?")
        if name in ("tune", "new_image", "show"):
            return await self.run_direct_action(chat_id, name, post, draft, str(action.get("mode") or ""))
        pending = PendingAction(secrets.token_hex(4), name, post.id, draft=draft)
        if name in ("schedule", "reschedule"):
            pending.when_spec = str(action.get("when") or "").strip().lower()
            pending.when = self.resolve_when(pending.when_spec)
            if pending.when is None:
                return await self.tell(chat_id, WHEN_HINT)
        self.pending[chat_id] = pending
        about = (post.draft(post.chosen or draft or "a").strip().splitlines() or [""])[0][:90] or (post.topic or post.brief)[:90]
        question = {
            "schedule": f"Schedule this for {format_ist(pending.when)}?" if pending.when else "",
            "reschedule": f"Move this to {format_ist(pending.when)}?" if pending.when else "",
            "unschedule": "Unschedule this? It goes back to waiting for a time.",
            "discard": "Discard this? It will never post.",
        }[name]
        keyboard = [[("✅ Yes", f"act:yes:{pending.nonce}"), ("✖ No", f"act:no:{pending.nonce}")]]
        self.memory.add(chat_id, "assistant", f"(asked to confirm: {question})")
        await self.say(chat_id, f"{question}\n\n“{about}”", keyboard)

    async def tell(self, chat_id: int, text: str) -> None:
        self.memory.add(chat_id, "assistant", text)
        await self.say(chat_id, text)

    def resolve_when(self, spec: str | None) -> datetime | None:
        now = self.now()
        if spec in RELATIVE_TIMES:
            return now + RELATIVE_TIMES[spec]
        return parse_when(spec or "", now)

    async def resolve_post(self, chat_id: int, ref: Any, statuses: tuple[str, ...]) -> Post | list[Post] | str:
        """The post an action means: by ref (uuid prefix from a lookup), or the only one in
        a matching state. Otherwise the real candidates to pick from (a ref the model made
        up matches nothing, so he picks from what actually exists), never a guess."""
        candidates = [p for status in statuses for p in await self.db.posts_with_status(chat_id, status, 10)]  # type: ignore[arg-type]
        ref = str(ref or "").strip().lstrip("#").lower().removeprefix("ref").strip()
        matches = [p for p in candidates if p.id.startswith(ref)] if len(ref) >= 4 else []
        if len(matches) == 1:
            return matches[0]
        if len(candidates) == 1 and not ref:
            return candidates[0]
        if not candidates:
            what = "scheduled post" if statuses == ("queued",) else "draft" if statuses == ("awaiting_choice",) else "post"
            return f"There's no {what} for that right now."
        return (matches or candidates)[:MAX_CHOICES]

    async def ask_which(self, chat_id: int, action: dict[str, Any], posts: list[Post]) -> None:
        """One button per real post; the tap carries on with the same action."""
        nonce = secrets.token_hex(4)
        self.choosing[chat_id] = (nonce, action, [p.id for p in posts])
        lines, keyboard = [], []
        for i, p in enumerate(posts, 1):
            when = f"{format_ist(p.scheduled_at)} — " if p.scheduled_at else ""
            lines.append(f"{i}. {when}{(p.topic or p.brief)[:90]}")
            keyboard.append([(f"{i}. {(p.topic or p.brief)[:40]}", f"act:pick:{nonce}:{i}")])
        text = "Which one?\n\n" + "\n".join(lines)
        self.memory.add(chat_id, "assistant", text)
        await self.say(chat_id, text, keyboard)

    async def run_direct_action(self, chat_id: int, name: str, post: Post, draft: str | None, mode: str) -> None:
        """Same as tapping the button under the post: these only touch a waiting draft."""
        if name == "tune" and mode not in TUNE_LABELS:
            return await self.tell(chat_id, "Shorter, a new hook, or bolder?")
        cb = {
            "tune": Callback("tune", f"{draft}-{mode}", post.id),
            "new_image": Callback("img", None, post.id),
            "show": Callback("show", None, post.id),
        }[name]
        if post.id in self.regenerating:
            return await self.tell(chat_id, "I'm still working on that draft — wait for the update.")
        self.memory.add(chat_id, "assistant", f"({name} on draft {post_ref(post)})")
        await self.route_callback(chat_id, cb, post)

    async def on_action_button(self, chat_id: int, data: str) -> str:
        _, choice, nonce, index = (data.split(":") + ["", "", ""])[:4]
        if choice == "pick":
            waiting = self.choosing.get(chat_id)
            if waiting is None or waiting[0] != nonce or not index.isdigit() or not 1 <= int(index) <= len(waiting[2]):
                return "Expired"
            del self.choosing[chat_id]
            await self.chat_action(chat_id, {**waiting[1], "post": waiting[2][int(index) - 1]})
            return "Picked"
        pending = self.pending.get(chat_id)
        if pending is None or pending.nonce != nonce:
            return "Expired"
        del self.pending[chat_id]
        if choice != "yes":
            await self.tell(chat_id, "Okay, left it as it is.")
            return "Cancelled"
        post = await self.db.get_post(pending.post_id)
        if post is None or post.chat_id != chat_id or post.status not in ACTION_STATUSES[pending.name]:
            await self.say(chat_id, "That post already moved on — nothing changed.")
            return "Expired"
        if post.id in self.regenerating:
            await self.say(chat_id, "I'm still working on that draft — try again once the update lands.")
            return "Working on it"
        when = pending.when
        if pending.when_spec in RELATIVE_TIMES:
            when = self.resolve_when(pending.when_spec)
        elif when is not None and when <= self.now():
            await self.say(chat_id, "That time has passed — nothing changed. Tell me a new time.")
            return "Expired"
        if pending.name == "schedule":
            if post.chosen != pending.draft and await self.db.update_post(post.id, {"chosen": pending.draft}, status="awaiting_choice") is None:
                return "Expired"
            return await self.queue(chat_id, post.id, when)  # type: ignore[arg-type]
        if pending.name == "reschedule":
            if await self.db.update_post(post.id, {"scheduled_at": when}, status="queued") is None:
                await self.say(chat_id, "Too late — it's already posting.")
                return "Too late — already posting"
            await self.say(chat_id, queued_text(post.chosen, when, not post.draft_b) + " (moved)", queued_keyboard(post.id))  # type: ignore[arg-type]
            await self.warn_if_crowded(chat_id, post.id, when)  # type: ignore[arg-type]
            return "Moved"
        cb = Callback("unq" if pending.name == "unschedule" else "drop", None, post.id)
        return await self.route_callback(chat_id, cb, post) or "Done"

    async def on_state_reply(self, chat_id: int, state: ChatState, text: str) -> None:
        action = state.pending_action
        if action == "awaiting_edit_a":
            return await self.save_edit(chat_id, state, "a", text)
        if action == "awaiting_edit_b":
            return await self.save_edit(chat_id, state, "b", text)
        if action == "awaiting_custom_time":
            return await self.save_custom_time(chat_id, state, text)
        if action == "awaiting_stats":
            return await self.save_stats(chat_id, state, text)
        if action == "awaiting_post_url":
            return await self.save_post_url(chat_id, state, text)
        if action == "awaiting_brief":
            return await self.save_brief(chat_id, text)
        if action == "awaiting_interview":
            return await self.interview_answer(chat_id, text)
        if action == "awaiting_comment":
            await self.db.clear_chat_state(chat_id)
            self.spawn(self.comment_flow(chat_id, text))
            return None

    # generation --------------------------------------------------------------------
    async def start_generation(self, chat_id: int, brief: str, echo: bool = False) -> None:
        """echo: say what the drafts will be about when the brief was worded by the bot
        (extracted from "make a post about …", written by the chat model, or from the news)."""
        if len(brief) < MIN_TOPIC_CHARS:
            return await self.say(chat_id, "What should the post be about? Send a topic or a link.")
        if len(brief) > MAX_BRIEF_CHARS:
            return await self.say(
                chat_id,
                f"That's over {MAX_BRIEF_CHARS} characters — it looks like a full post, not a brief. "
                "If it's an edited draft, your edit window expired: tap Edit again, then resend. "
                "Otherwise send a shorter brief.",
            )
        running = self.active_runs.get(chat_id)
        if running is not None:  # one brief at a time per chat — never start a second run underneath it
            return await self.say(chat_id, self.run_status_text(running))
        about = f" about:\n{brief}\n\n" if echo else ". "
        await self.say(chat_id, f"On it — researching and writing your post{about}Takes 3–5 minutes.")
        run = ActiveRun(brief=brief, started_at=self.now())
        self.active_runs[chat_id] = run
        run.task = self.spawn(self._generate(chat_id, brief, run))

    async def _generate(self, chat_id: int, brief: str, run: ActiveRun) -> None:
        try:
            await run_brief(self.svc, chat_id, brief, on_stage=lambda s: setattr(run, "stage", s))
        except Exception as exc:
            log.exception("generation_failed")
            await self.say(chat_id, f"Generation failed: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            if self.active_runs.get(chat_id) is run:
                del self.active_runs[chat_id]

    def run_status_text(self, run: ActiveRun) -> str:
        mins = max(0, int((self.now() - run.started_at).total_seconds() // 60))
        elapsed = f"{mins} min ago" if mins else "just now"
        return f"⏳ Still on it — started {elapsed}, currently {run.stage}. I'll send it here the moment it's ready."

    def run_status_for_model(self, run: ActiveRun | None) -> str | None:
        if run is None:
            return None
        mins = max(0, int((self.now() - run.started_at).total_seconds() // 60))
        return f'A draft is already running: started {mins} min ago on "{run.brief[:120]}", currently {run.stage}.'

    async def cancel_run(self, chat_id: int, run: ActiveRun) -> None:
        if run.task is not None:
            run.task.cancel()
        if self.active_runs.get(chat_id) is run:
            del self.active_runs[chat_id]
        await self.say(chat_id, f'Cancelled — stopped the draft on "{run.brief[:80]}".')

    async def _regen(self, post: Post) -> None:
        try:
            if await regenerate(self.svc, post) is None:
                await self.say(post.chat_id, "Draft already locked — regenerated drafts discarded.")
        except Exception as exc:
            log.exception("regen_failed", extra={"post_id": post.id})
            await self.say(post.chat_id, f"Regenerate failed: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            self.regenerating.discard(post.id)

    # callbacks ---------------------------------------------------------------------
    async def on_callback(self, chat_id: int, callback_id: str, data: str | None, message_id: int | None = None) -> None:
        answer: str | None = "Expired"
        action = (data or "").split(":", 1)[0]
        try:
            if data and data.startswith("idea:"):
                answer = await self.on_idea(chat_id, data)
                return
            if data in ("iv:skip", "iv:never", "iv:done"):
                answer = await self.interview_button(chat_id, data[3:])
                return
            if data and data.startswith("act:"):
                answer = await self.on_action_button(chat_id, data)
                if message_id is not None:
                    await self._clear_buttons(chat_id, message_id)  # yes or no, this proposal is spent
                return
            if data == "status:refresh":
                await self.status_flow(chat_id)
                answer = "Updated"
                return
            if data and data.startswith("menu:/") and data[5:] in QUICK_COMMANDS:
                await self.on_command(chat_id, data[5:], "")
                answer = None
                return
            cb = parse_callback(data)
            post = await self.db.get_post(cb.post_id) if cb else None
            if cb is None or post is None or post.chat_id != chat_id:
                return
            if not guard_allows(cb, post.status, post.chosen):
                return
            if post.id in self.regenerating and cb.action in ("pick", "time", "edit", "regen", "tune", "img"):
                answer = "Working on it — wait for the update"
                return
            answer = await self.route_callback(chat_id, cb, post)
            if message_id is not None and cb.action in STALE_AFTER and answer not in DEAD_ANSWERS:
                await self._clear_buttons(chat_id, message_id)  # these buttons did their job
        finally:
            await self.msg.answer_callback(callback_id, answer)  # always, so the button stops spinning
            log.info("button", extra={"action": action, "result": answer})
            if message_id is not None and answer in DEAD_ANSWERS:
                # A toast is easy to miss: say it in the chat, clear the dead buttons, show where things stand.
                await self._clear_buttons(chat_id, message_id)
                await self.say(chat_id, "That button is out of date (the post already moved on). Here's where everything stands:")
                await self.status_flow(chat_id)

    async def _clear_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            await self.msg.edit_keyboard(chat_id, message_id, None)
        except Exception:  # already edited, too old, or deleted: nothing to clear
            log.info("clear_buttons_skipped")

    async def route_callback(self, chat_id: int, cb: Callback, post: Post) -> str | None:
        now = self.now()
        if cb.action == "pick":
            if not post.draft(cb.arg or "").strip():  # a one-post row has no draft B to approve
                return "Expired"
            if await self.db.update_post(post.id, {"chosen": cb.arg}, status="awaiting_choice") is None:
                return "Expired"
            picked = "✅ Approved." if not post.draft_b else f"Draft {cb.arg.upper()} picked."
            await self.say(chat_id, f"{picked} When should it go out?\n{TIME_HINT}", time_keyboard(post.id))
            return f"Draft {cb.arg.upper()}"
        if cb.action == "time":
            if cb.arg == "custom":
                await self.db.set_chat_state(chat_id, "awaiting_custom_time", post.id, now)
                await self.say(chat_id, "Send time as HH:MM (IST)")
                return None
            when = {
                "now": lambda: now + NOW_DELAY,
                "30m": lambda: now + timedelta(minutes=30),
                "6pm": lambda: next_6pm(now),
                "9pm": lambda: next_ist_occurrence(21, 0, now),
                "9am": lambda: next_9am(now),
            }[cb.arg]()
            return await self.queue(chat_id, post.id, when)
        if cb.action == "regen":
            self.regenerating.add(post.id)
            await self.say(chat_id, "🔄 Writing a new version…")
            self.spawn(self._regen(post))
            return "Regenerating"
        if cb.action == "edit":
            await self.db.set_chat_state(chat_id, f"awaiting_edit_{cb.arg}", post.id, now)  # type: ignore[arg-type]
            await self.say(chat_id, f"Send your edited version of draft {cb.arg.upper()}")
            return None
        if cb.action == "live":
            await self.db.set_chat_state(chat_id, "awaiting_post_url", post.id, now)
            await self.say(chat_id, "Paste the LinkedIn post URL.")
            return None
        if cb.action == "tune":
            which, mode = cb.arg.split("-", 1)  # type: ignore[union-attr]
            self.regenerating.add(post.id)
            await self.say(chat_id, f"{TUNE_LABELS[mode]} draft {which.upper()}…")
            self.spawn(self._tune(post, which, mode))
            return "On it"
        if cb.action == "img":
            self.regenerating.add(post.id)
            await self.say(chat_id, "🖼 Making a new image…")
            self.spawn(self._new_image(post))
            return "New image"
        if cb.action == "drop":
            if await self.db.update_post(post.id, {"status": "failed", "error": "discarded by user"}, status=post.status) is None:
                return "Expired"
            await self.say(chat_id, "🗑 Discarded. It will never post.")
            return "Discarded"
        if cb.action == "show":
            await preview(self.svc, post)
            return None
        if cb.action == "unq":
            if await self.db.update_post(post.id, {"status": "awaiting_choice", "scheduled_at": None}, status="queued") is None:
                return "Too late — already posting"
            await self.say(chat_id, f"↩️ Unscheduled. Draft {(post.chosen or '?').upper()} is waiting again — pick a new time:", time_keyboard(post.id))
            return "Unscheduled"
        if cb.action == "asap":
            when = now + NOW_DELAY
            if await self.db.update_post(post.id, {"scheduled_at": when}, status="queued") is None:
                return "Too late — already posting"
            await self.say(chat_id, queued_text(post.chosen, when, not post.draft_b) + " (moved up)", queued_keyboard(post.id))
            return "Moved up"
        if cb.action == "notlive":
            fields = {"status": "queued", "retry_count": 0, "claimed_at": None, "alerted_at": None}
            if await self.db.update_post(post.id, fields, status="posting") is None:
                return "Expired"
            await self.say(chat_id, "Re-queued — the dispatcher will post it on its next run (≤5 min).")
            return "Re-queued"
        return "Expired"

    async def queue(self, chat_id: int, post_id: str, when: datetime) -> str:
        updated = await self.db.update_post(
            post_id, {"scheduled_at": when, "status": "queued"}, status="awaiting_choice", require_chosen=True
        )
        if updated is None:
            await self.say(chat_id, "Expired — that draft is no longer waiting for a time.")
            return "Expired"
        await self.say(chat_id, queued_text(updated.chosen, when, not updated.draft_b), queued_keyboard(post_id))
        await self.warn_if_crowded(chat_id, post_id, when)
        return "Queued"

    async def warn_if_crowded(self, chat_id: int, post_id: str, when: datetime) -> None:
        """Two posts within 24 hours split each other's reach."""
        others = await self.db.posts_with_status(chat_id, "queued", 10) + await self.db.last_posted(5)
        for p in others:
            at = p.scheduled_at if p.status == "queued" else p.posted_at
            if p.id != post_id and at is not None and abs(at - when) < CROWDED:
                await self.say(chat_id, f"Heads-up: another post goes out {format_ist(at)}, within 24 hours of this one. "
                                        "Two posts that close split each other's reach; consider moving one.")
                return

    async def on_idea(self, chat_id: int, data: str) -> str:
        ideas = self.ideas.get(chat_id) or []
        n = data.split(":", 1)[1]
        if not n.isdigit() or not 1 <= int(n) <= len(ideas):
            return "Expired — run /ideas again"
        await self.start_generation(chat_id, ideas[int(n) - 1], echo=True)
        return f"Drafting idea {n}"

    async def _tune(self, post: Post, which: str, mode: str) -> None:
        """✂️ / 🎣 / 🔥 on one draft. Kept only if it adds no unverifiable number."""
        try:
            current = post.draft(which)
            research = post.research or {}
            hooks = [str(h) for h in (research.get("outline") or {}).get("hooks") or []]
            edited = airy(await self.svc.writer.tune(current, mode, hooks))
            corpus = research_corpus(research, post.brief)
            if len(unverified_numbers(edited, corpus)) > len(unverified_numbers(current, corpus)):
                return await self.say(post.chat_id, "That edit added a number I can't verify — kept the original. Try again or ✏️ Edit.")
            facts = audit_facts(research)
            sources = research_corpus({"results": research.get("results") or []})
            flags = (await audit_flags(self.svc.writer, edited, post.brief, facts, sources))[:3]
            research = {**research, "claim_flags": {**(research.get("claim_flags") or {}), which: flags}}
            fields = {f"draft_{which}": edited, f"edited_{which}": False, "chosen": None, "research": research}
            updated = await self.db.update_post(post.id, fields, status="awaiting_choice")
            if updated is None:
                return await self.say(post.chat_id, "Draft already locked")
            await send_draft(
                self.msg, post.chat_id, post.id, which, edited,
                unverified_numbers(edited, corpus), copied_for({which: edited}, research)[which], flags, not post.draft_b,
            )
        except Exception as exc:
            log.exception("tune_failed", extra={"post_id": post.id})
            await self.say(post.chat_id, f"Edit failed: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            self.regenerating.discard(post.id)

    async def _new_image(self, post: Post) -> None:
        try:
            drafts = {"a": post.draft("a"), "b": post.draft("b")}
            images = await make_images(self.svc, drafts, (post.research or {}).get("outline"))
            urls = await upload_images(self.svc, post.id, images)
            if await self.db.update_post(post.id, {"image_a_url": urls["a"], "image_b_url": urls["b"]}, status="awaiting_choice") is None:
                return await self.say(post.chat_id, "Draft already locked — new image discarded.")
            await self.msg.send_photo(post.chat_id, urls["a"])
            await self.say(post.chat_id, "🖼 New image ready.", draft_keyboard(post.id, two=bool(post.draft_b)))
        except Exception as exc:
            log.exception("new_image_failed", extra={"post_id": post.id})
            await self.say(post.chat_id, f"New image failed: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            self.regenerating.discard(post.id)

    # state replies -----------------------------------------------------------------
    async def save_edit(self, chat_id: int, state: ChatState, which: str, text: str) -> None:
        post = await self.db.get_post(state.post_id) if state.post_id else None
        if post is None or post.status != "awaiting_choice":  # A3
            await self.db.clear_chat_state(chat_id)
            return await self.say(chat_id, "Draft already locked")
        if post.id in self.regenerating:  # the regen would overwrite this edit
            await self.db.clear_chat_state(chat_id)
            return await self.say(chat_id, "Regenerating — wait for the new drafts, then tap Edit again.")
        if len(text) < MIN_EDIT_CHARS:
            return await self.say(chat_id, "That's too short for a post — send the full edited draft, or /cancel.")
        if len(escape_little_text(text)) > LINKEDIN_MAX_CHARS:
            return await self.say(chat_id, f"Too long for LinkedIn ({LINKEDIN_MAX_CHARS} chars incl. escaping). Trim it and resend.")
        await self.db.clear_chat_state(chat_id)
        # chosen resets: a pick made before the edit referred to the old text.
        fields = {f"draft_{which}": text, f"edited_{which}": True, "chosen": None}
        updated = await self.db.update_post(post.id, fields, status="awaiting_choice")
        if updated is None:
            return await self.say(chat_id, "Draft already locked")
        await preview(self.svc, updated)

    async def save_custom_time(self, chat_id: int, state: ChatState, text: str) -> None:
        when = custom_time(text, self.now())
        if when is None:
            return await self.say(chat_id, "Couldn't read that. Send time as HH:MM (IST), e.g. 18:30 — or /cancel.")
        await self.db.clear_chat_state(chat_id)
        if state.post_id:
            await self.queue(chat_id, state.post_id, when)

    async def save_post_url(self, chat_id: int, state: ChatState, text: str) -> None:
        """A2 'Live ✓': you confirmed it's live; record the URL and admit to corpus."""
        if not looks_like_post_url(text):
            return await self.say(chat_id, "That doesn't look like a LinkedIn URL (https://www.linkedin.com/...). Try again or /cancel.")
        await self.db.clear_chat_state(chat_id)
        fields = {"status": "posted", "posted_at": self.now(), "post_url": text.strip(), "error": None}
        updated = await self.db.update_post(state.post_id or "", fields, status="posting")
        if updated is None:
            return await self.say(chat_id, "That post is no longer in 'posting' — nothing changed.")
        try:
            await corpus.admit_on_post(self.db, self.svc.embedder, updated)
        except Exception:
            log.exception("corpus_admission_failed", extra={"post_id": updated.id})
        await self.say(chat_id, f"Marked posted ✓ {updated.post_url}")

    # /stats (§5.3) -------------------------------------------------------------------
    async def stats_flow(self, chat_id: int) -> None:
        rows = await self.db.last_posted(5)
        if not rows:
            return await self.say(chat_id, "No posted posts yet.")
        # The newest listed row anchors the list, so the reply maps to exactly these rows.
        await self.db.set_chat_state(chat_id, "awaiting_stats", rows[0].id, self.now())
        lines = [
            f"{i}. {r.posted_at.astimezone(IST).strftime('%d %b') if r.posted_at else '?'} — {r.post_url or '(no url)'}"
            for i, r in enumerate(rows, 1)
        ]
        await self.say(
            chat_id,
            "Reply with likes and comments, one line per post in this order ('-' to skip):\n\n"
            + "\n".join(lines)
            + "\n\nExample:\n120 14\n-\n85 9",
        )

    async def save_stats(self, chat_id: int, state: ChatState, text: str) -> None:
        anchor = await self.db.get_post(state.post_id) if state.post_id else None
        if anchor is None or anchor.posted_at is None:
            await self.db.clear_chat_state(chat_id)
            return await self.say(chat_id, "That stats session is stale — send /stats again.")
        rows = await self.db.last_posted(5, before=anchor.posted_at)
        parsed = parse_stats_lines(text, len(rows))
        if isinstance(parsed, str):
            return await self.say(chat_id, parsed + " Try again or /cancel.")
        await self.db.clear_chat_state(chat_id)
        results = []
        for i, (row, stat) in enumerate(zip(rows, parsed, strict=True), 1):
            if stat is None:
                results.append(f"{i}. skipped")
                continue
            outcome = await corpus.record_stats(self.db, self.svc.embedder, row, *stat)
            results.append(f"{i}. {sum(stat)} engagement — {outcome.replace('_', ' ')}")
        await self.say(chat_id, "Stats saved:\n" + "\n".join(results))


# ── cron modes (§5.4) ────────────────────────────────────────────────────────
async def nudge(svc: Services) -> None:
    assert svc.messenger
    chat_id = svc.settings.my_chat_id
    # The reply goes straight to drafts (no "make a post" needed) for the next 30 minutes.
    await svc.db.set_chat_state(chat_id, "awaiting_brief", None, utcnow())
    await svc.messenger.send_text(chat_id, "What's today's topic? Send a topic or a link — or say \"you pick\".")


async def fallback(svc: Services, now: datetime) -> str:
    """Drafts only — never posts. Skips if any brief was sent today (IST date)."""
    assert svc.messenger
    chat_id = svc.settings.my_chat_id
    if await svc.db.any_post_since(ist_day_start(now)):
        log.info("fallback_skipped_brief_exists")
        return "skipped"
    topic = await svc.researcher.news_topic(svc.settings.keywords, now.astimezone(IST).toordinal())
    if not topic:
        await svc.messenger.send_text(chat_id, "No brief today and no fallback news topic found. Send a brief anytime.")
        return "no_topic"
    await svc.messenger.send_text(chat_id, f"No brief today — drafting from the news:\n{topic}")
    try:
        await run_brief(svc, chat_id, topic)
    except Exception as exc:
        log.exception("fallback_failed")
        await svc.messenger.send_text(chat_id, f"Fallback generation failed: {type(exc).__name__}: {str(exc)[:300]}")
        return "failed"
    return "drafted"


async def token_check(svc: Services, now: datetime) -> str:
    assert svc.messenger
    chat_id = svc.settings.my_chat_id
    issued = parse_ts(await svc.db.get_setting("linkedin_token_issued_at"))
    if issued is None or not await svc.db.get_setting("linkedin_access_token"):
        await svc.messenger.send_text(chat_id, "⚠ No LinkedIn token stored — nothing can post.\n\n" + REAUTH_STEPS)
        return "missing"
    age = (now - issued).days
    if age > TOKEN_WARN_DAYS:
        await svc.messenger.send_text(chat_id, f"⚠ LinkedIn token is {age} days old (expires ~60).\n\n" + REAUTH_STEPS)
        return "warned"
    log.info("token_ok", extra={"age_days": age})
    return "ok"


async def run_cron(mode: str, settings: Settings) -> None:
    async with Bot(settings.telegram_bot_token.get_secret_value(), request=telegram_request(settings)) as bot:
        svc = await build_services(settings, TelegramMessenger(bot))
        now = utcnow()
        if mode == "nudge":
            await nudge(svc)
        elif mode == "fallback":
            await fallback(svc, now)
        elif mode == "token-check":
            await token_check(svc, now)
        if svc.recorder:
            await svc.recorder.drain()  # flush usage-ledger rows before the process exits
    log.info("cron_done", extra={"mode": mode})


def run_polling(settings: Settings) -> None:
    async def post_init(app: Application) -> None:
        svc = await build_services(settings, TelegramMessenger(app.bot))
        app.bot_data["handler"] = BotHandler(svc)
        try:  # the "/" menu in Telegram
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        except Exception:
            log.warning("set_commands_failed")
        log.info("bot_started")

    async def handle(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(update, Update):
            await context.bot_data["handler"].on_update(update)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("ptb_error", exc_info=context.error)

    t = settings.http_timeout
    app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .connect_timeout(10)
        .read_timeout(t)
        .write_timeout(t)
        .media_write_timeout(60)
        .pool_timeout(10)
        .get_updates_read_timeout(t + 20)
        .post_init(post_init)
        .build()
    )
    app.add_handler(TypeHandler(Update, handle))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=["message", "callback_query"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--nudge", action="store_true")
    group.add_argument("--fallback", action="store_true")
    group.add_argument("--token-check", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    mode = "nudge" if args.nudge else "fallback" if args.fallback else "token-check" if args.token_check else None
    if mode:
        asyncio.run(run_cron(mode, settings))
    else:
        run_polling(settings)


if __name__ == "__main__":
    main()
