"""Telegram UI: callback_data build/parse, status guards, keyboards, and the
preview send sequence (SPEC §5.1 step 11). All text is plain — parse_mode is
never set (A4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.request import HTTPXRequest

if TYPE_CHECKING:
    from app.config import Settings

MAX_CALLBACK_BYTES = 64  # Telegram hard limit on callback_data

# action -> allowed args (None = the action takes no arg)
_ACTIONS: dict[str, frozenset[str] | None] = {
    "pick": frozenset({"a", "b"}),
    "time": frozenset({"now", "30m", "6pm", "9pm", "9am", "custom"}),
    "regen": None,
    "edit": frozenset({"a", "b"}),
    "live": None,  # A2: stuck row confirmed live
    "notlive": None,  # A2: stuck row confirmed not live
    "tune": frozenset(f"{w}-{m}" for w in "ab" for m in ("short", "hook", "bold")),  # ✂️ 🎣 🔥 on one draft
    "img": None,  # 🖼 new image
    "drop": None,  # 🗑 discard
    "show": None,  # 👀 re-send a waiting post's preview (/drafts)
    "unq": None,  # ↩️ unschedule a queued post
    "asap": None,  # ⚡ move a queued post to now
}


@dataclass(frozen=True)
class Callback:
    action: str
    arg: str | None
    post_id: str


def build_callback(action: str, post_id: str | UUID, arg: str | None = None) -> str:
    allowed = _ACTIONS.get(action, "missing")
    if allowed == "missing":
        raise ValueError(f"unknown callback action {action!r}")
    if (allowed is None) != (arg is None) or (allowed is not None and arg not in allowed):
        raise ValueError(f"bad arg {arg!r} for {action!r}")
    pid = str(UUID(str(post_id)))
    data = f"{action}:{arg}:{pid}" if arg else f"{action}:{pid}"
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError("callback_data exceeds 64 bytes")
    return data


def parse_callback(data: str | None) -> Callback | None:
    """Strict inverse of build_callback. Anything malformed → None."""
    if not data or len(data.encode()) > MAX_CALLBACK_BYTES:
        return None
    parts = data.split(":")
    action = parts[0]
    if action not in _ACTIONS:
        return None
    allowed = _ACTIONS[action]
    if allowed is None:
        if len(parts) != 2:
            return None
        arg, raw_id = None, parts[1]
    else:
        if len(parts) != 3 or parts[1] not in allowed:
            return None
        arg, raw_id = parts[1], parts[2]
    try:
        pid = str(UUID(raw_id))
    except ValueError:
        return None
    return Callback(action, arg, pid)


# ── Status guards (SPEC §5.2 + A2) ──────────────────────────────────────────
def guard_allows(cb: Callback, status: str | None, chosen: str | None) -> bool:
    """True only if the post is in the state this button was issued for."""
    if status is None:
        return False
    if cb.action in ("pick", "regen", "edit", "tune", "img", "show"):
        return status == "awaiting_choice"
    if cb.action == "drop":
        return status in ("awaiting_choice", "queued")
    if cb.action in ("unq", "asap"):
        return status == "queued"
    if cb.action == "time":
        return status == "awaiting_choice" and chosen is not None
    if cb.action in ("live", "notlive"):
        return status == "posting"
    return False


# ── Keyboards: rows of (label, callback_data) ────────────────────────────────
Button = tuple[str, str]
Keyboard = list[list[Button]]


def draft_keyboard(post_id: str, two: bool = False) -> Keyboard:
    """Under the whole preview: redo the parts (and, for older two-draft posts, pick one)."""
    rows: Keyboard = []
    if two:
        rows.append([("✅ Post A", build_callback("pick", post_id, "a")), ("✅ Post B", build_callback("pick", post_id, "b"))])
    rows.append([("🔄 New version", build_callback("regen", post_id)), ("🖼 New image", build_callback("img", post_id))])
    rows.append([("🗑 Discard", build_callback("drop", post_id))])
    return rows


def draft_tools(post_id: str, which: str, single: bool = True) -> Keyboard:
    """Under the post: approve it (gate 1), edit it, or tune it."""
    name = "" if single else f" {which.upper()}"
    return [
        [
            ("✂️ Shorter", build_callback("tune", post_id, f"{which}-short")),
            ("🎣 New hook", build_callback("tune", post_id, f"{which}-hook")),
            ("🔥 Bolder", build_callback("tune", post_id, f"{which}-bold")),
        ],
        [(f"✅ Post{name or ' this'}", build_callback("pick", post_id, which)), (f"✏️ Edit{name}", build_callback("edit", post_id, which))],
    ]


def queued_keyboard(post_id: str) -> Keyboard:
    return [
        [("⚡ Post now", build_callback("asap", post_id)), ("↩️ Unschedule", build_callback("unq", post_id))],
        [("🗑 Discard", build_callback("drop", post_id))],
    ]


def waiting_keyboard(post_id: str) -> Keyboard:
    return [[("👀 Show", build_callback("show", post_id)), ("🗑 Discard", build_callback("drop", post_id))]]


def time_keyboard(post_id: str) -> Keyboard:
    return [
        [("⚡ Now", build_callback("time", post_id, "now")), ("⏱ In 30 min", build_callback("time", post_id, "30m"))],
        [("🕕 6 PM", build_callback("time", post_id, "6pm")), ("🌙 9 PM", build_callback("time", post_id, "9pm"))],
        [("🌅 9 AM", build_callback("time", post_id, "9am")), ("✍️ Custom", build_callback("time", post_id, "custom"))],
    ]


def stuck_keyboard(post_id: str) -> Keyboard:
    return [[("Live ✓", build_callback("live", post_id)), ("Not live", build_callback("notlive", post_id))]]


# ── Messaging abstraction (TelegramMessenger below; faked in tests) ─────────
class Messenger(Protocol):
    async def send_text(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> None: ...

    async def send_photo(self, chat_id: int, photo_url: str) -> None: ...

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...

    async def send_menu(self, chat_id: int, text: str, rows: list[list[str]]) -> None: ...

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None: ...


# ── PTB-backed Messenger (plain text only — parse_mode is never set, A4) ────
def to_markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in keyboard])


class TelegramMessenger:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_text(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=to_markup(keyboard))

    async def send_photo(self, chat_id: int, photo_url: str) -> None:
        await self._bot.send_photo(chat_id=chat_id, photo=photo_url)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self._bot.answer_callback_query(callback_query_id=callback_id, text=text)

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        """Replace (or with None, remove) the buttons on a message already sent."""
        await self._bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=to_markup(keyboard))

    async def send_menu(self, chat_id: int, text: str, rows: list[list[str]]) -> None:
        """The quick-tap menu that stays under the message box."""
        markup = ReplyKeyboardMarkup([[KeyboardButton(label) for label in row] for row in rows], resize_keyboard=True, is_persistent=True)
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


def telegram_request(settings: Settings) -> HTTPXRequest:
    t = settings.http_timeout
    return HTTPXRequest(connect_timeout=10, read_timeout=t, write_timeout=t, pool_timeout=10, media_write_timeout=60)


# ── Preview send sequence (SPEC §5.1 step 11) ────────────────────────────────
TELEGRAM_TEXT_LIMIT = 4000  # real limit 4096; leave headroom
DRAFT_LABELS = {"a": "DRAFT A — story / founder-POV", "b": "DRAFT B — contrarian / insight-list"}
SINGLE_LABEL = "YOUR POST"
FOOTER = "Happy with it? Tap ✅ Post this, then pick a time. Or tune it above."


def draft_message(
    which: str, text: str, unverified: list[str], copied: bool = False, flags: list[str] | None = None, single: bool = False
) -> str:
    from app.style_check import CHAR_MAX, HOOK_MAX, post_hook

    words, chars, hook = len(text.split()), len(text), len(post_hook(text))
    size = f"{words} words · {chars} chars" + (" · long" if chars > CHAR_MAX else "")
    hook_note = f"hook {hook}/{HOOK_MAX}" + (" — cut off on mobile" if hook > HOOK_MAX else " ✓")
    msg = f"{SINGLE_LABEL if single else DRAFT_LABELS[which]}\n{size} · {hook_note}\n\n{text}"
    if unverified:
        msg += f"\n\n⚠ unverified numbers: {', '.join(unverified)}"
    if copied:
        msg += "\n\n⚠ copies sentences from a source — edit or regenerate before posting"
    for claim in (flags or [])[:3]:
        short = claim if len(claim) <= 140 else claim[:139].rsplit(" ", 1)[0] + "…"
        msg += f"\n⚠ check: “{short}” — not in your brief or background"
    return msg


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


async def send_preview(
    messenger: Messenger,
    chat_id: int,
    post_id: str,
    drafts: dict[str, str],
    images: dict[str, str | None],
    unverified: dict[str, list[str]],
    copied: dict[str, bool] | None = None,
    flags: dict[str, list[str]] | None = None,
) -> None:
    """Separate calls, in order: photo(s) → draft A → draft B → keyboard. One photo when
    both drafts share the image. (Buttons can't attach to media groups; captions cap at 1024.)"""
    present = [w for w in ("a", "b") if (drafts.get(w) or "").strip()]
    single = len(present) == 1
    for url in dict.fromkeys(images.get(w) for w in present if images.get(w)):
        await messenger.send_photo(chat_id, url)
    for which in present:
        await send_draft(
            messenger, chat_id, post_id, which, drafts[which], unverified.get(which, []),
            (copied or {}).get(which, False), (flags or {}).get(which), single,
        )
    await messenger.send_text(chat_id, FOOTER if single else "Pick a draft:", draft_keyboard(post_id, two=not single))


async def send_draft(
    messenger: Messenger, chat_id: int, post_id: str, which: str, text: str, unverified: list[str],
    copied: bool = False, flags: list[str] | None = None, single: bool = True,
) -> None:
    """One post (or one of an older post's two drafts), with its buttons under the last chunk."""
    chunks = split_text(draft_message(which, text, unverified, copied, flags, single))
    for i, chunk in enumerate(chunks):
        await messenger.send_text(chat_id, chunk, draft_tools(post_id, which, single) if i == len(chunks) - 1 else None)
