"""Conversation mode for the Telegram bot.

Plain messages get a chat reply. Asking for a post in your own words ("make a post
about X") opens a short conversation that pins down the angle before anything is
drafted; the explicit short forms ("draft: X", "/new X", a pasted link) start the
draft pipeline straight away. Chat never posts or schedules anything: drafts still
need a human pick AND a time.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field

MIN_TOPIC_CHARS = 3  # "RAG" is a fine topic when the request is explicit
HISTORY_TURNS = 24  # room for the back-and-forth that shapes a post; messages kept per chat (user + assistant), persisted to settings

_POLITE = r"(?:(?:hey|hi|ok|okay|please|pls|so|now|can\s+you|could\s+you|would\s+you|let'?s|go\s+ahead\s+and)[\s,]+)*"
_VERB = r"(?:make|write|draft|create|generate|prepare|craft|do|start|give\s+me)"
_NOUN_PREFIX = r"(?:(?:me|us|a|an|another|one|the|my|new|quick|today'?s|linkedin)\s+)*"
_REQUEST = re.compile(
    rf"^{_POLITE}{_VERB}\s+{_NOUN_PREFIX}(?:post|draft)s?\b[\s,.!?]*"
    r"(?:(?:about|on|for|regarding|around|covering)\b|[:\-—–])?\s*(?P<topic>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# Short forms. Bare "draft" must be followed by ':' / '-' / 'about' / 'on', so
# "draft A is better" stays chat. "post it" is about publishing, never a request.
_SHORT = re.compile(
    rf"^{_POLITE}(?:/draft|/post|draft\s*[:\-—–]|draft\s+(?:about|on)\b|post\s+(?:about|on)\b)\s*(?P<topic>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_DRAFT_THAT = re.compile(
    rf"^{_POLITE}(?:draft|write|make)\s+(?:it|this|that|one)(?:\s+(?:up|into\s+a\s+post))?[\s.!]*$", re.IGNORECASE
)
_REFERENCES = {"it", "this", "that", "one", "this one", "that one", "that idea", "this idea", "the idea", "the above"}
_YOU_PICK = re.compile(
    r"^(?:you\s+(?:pick|choose|decide)|surprise\s+me|anything|whatever|your\s+(?:choice|call)|pick\s+one|from\s+(?:the\s+)?news)\b",
    re.IGNORECASE,
)

_DECLINE = re.compile(
    r"^(?:no|nope|nah|not\s+(?:today|now)|skip(?:\s+today)?|later|nothing|none|pass|no\s+post(?:\s+today)?)\b[\s.!,]*(?:thanks|thank\s+you|thx)?[\s.!]*$",
    re.IGNORECASE,
)

# Checked only while a draft run is in progress (see BotHandler.active_runs), so this
# never shadows unrelated chat like "what's the status of AI regulation".
_STATUS_QUERY = re.compile(
    r"^(?:\?+|status\??|any\s+update\??|is\s+it\s+(?:done|ready)\??|how'?s?\s+it\s+going\??|"
    r"where'?s?(?:\s+is)?\s+(?:my|the)\s+draft\??|done\s+yet\??|progress\??)[\s.!]*$",
    re.IGNORECASE,
)
_CANCEL_REQUEST = re.compile(r"^(?:cancel|stop|abort|never\s*mind|forget\s+it)\b[\s.!]*$", re.IGNORECASE)
# A typed answer to a pending ✅/✖ question (see BotHandler.pending). Whole-message only,
# so "yes but make it shorter" goes to chat instead of confirming.
_YES = re.compile(
    r"^(?:y|ya|yes|yeah|yep|yup|sure|ok|okay|confirm(?:ed)?|do\s+it|go\s+ahead|go\s+for\s+it|please\s+do|"
    r"yes\s+please|yes\s+(?:all|all\s+of\s+them|do\s+it|discard|delete|go\s+ahead)|✅)[\s.!👍]*$",
    re.IGNORECASE,
)
_NO = re.compile(r"^(?:n|no|nope|nah|don'?t|do\s+not|cancel|keep\s+(?:it|them)|never\s*mind|✖|❌)[\s.!]*$", re.IGNORECASE)


@dataclass(frozen=True)
class DraftRequest:
    """topic=None: asked for a post without saying what about (or said "it/that").
    direct=False: asked in his own words ("make a post about X"), so the chat model talks
    the angle through first; True: an explicit command ("draft: X", "/new X")."""

    topic: str | None
    direct: bool = field(default=True, compare=False)


def parse_draft_request(text: str) -> DraftRequest | None:
    """Explicit post requests, matched without a model call. None → treat as chat."""
    t = text.strip()
    if _DRAFT_THAT.match(t):
        return DraftRequest(None)
    short = _SHORT.match(t)
    m = short or _REQUEST.match(t)
    if not m:
        return None
    topic = m.group("topic").strip().strip(" .!?,:;-—–").strip()
    if topic.lower() in _REFERENCES or len(topic) < MIN_TOPIC_CHARS:
        return DraftRequest(None)
    return DraftRequest(topic, direct=bool(short))


def wants_bot_to_pick(text: str) -> bool:
    return bool(_YOU_PICK.match(text.strip()))


class ChatMemory:
    """Last few messages per chat so "draft that" and "yes, do it" have context.
    The bot persists snapshots to settings (chat_history:<chat_id>): a restart
    keeps the conversation, never any post state."""

    def __init__(self, turns: int = HISTORY_TURNS) -> None:
        self._turns = turns
        self._chats: dict[int, deque[dict[str, str]]] = {}
        self._dirty: set[int] = set()  # chats with turns not yet saved

    def add(self, chat_id: int, role: str, content: str) -> None:
        self._chats.setdefault(chat_id, deque(maxlen=self._turns)).append({"role": role, "content": content})
        self._dirty.add(chat_id)

    def history(self, chat_id: int) -> list[dict[str, str]]:
        return list(self._chats.get(chat_id, ()))

    def has_context(self, chat_id: int) -> bool:
        return bool(self._chats.get(chat_id))

    def snapshot(self, chat_id: int) -> str:
        """JSON of the current window, for the settings table."""
        return json.dumps(self.history(chat_id))

    def load(self, chat_id: int, raw: str) -> bool:
        """Restore a snapshot. Returns False on anything unexpected."""
        try:
            msgs = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(msgs, list):
            return False
        valid = [
            {"role": m["role"], "content": m["content"]}
            for m in msgs
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        ]
        if not valid:
            return False
        self._chats[chat_id] = deque(valid[-self._turns :], maxlen=self._turns)
        self._dirty.discard(chat_id)
        return True

    def dirty_chats(self) -> list[int]:
        return list(self._dirty)

    def mark_saved(self, chat_id: int) -> None:
        self._dirty.discard(chat_id)


def is_decline(text: str) -> bool:
    """"not today", "skip", "no thanks" in reply to "what should the post be about?"."""
    return bool(_DECLINE.match(text.strip()))


def is_status_query(text: str) -> bool:
    """"?", "status?", "is it done?", "where's my draft?" — a check-in, not a new request."""
    return bool(_STATUS_QUERY.match(text.strip()))


def is_cancel_request(text: str) -> bool:
    """"cancel", "stop", "never mind" while a run is in progress."""
    return bool(_CANCEL_REQUEST.match(text.strip()))


def is_yes(text: str) -> bool:
    """"yes", "yep", "do it", "yes all" — typed instead of tapping ✅."""
    return bool(_YES.match(text.strip()))


def is_no(text: str) -> bool:
    """"no", "keep it", "never mind" — typed instead of tapping ✖."""
    return bool(_NO.match(text.strip()))


def short_label(text: str, limit: int = 28) -> str:
    """Fit a button label without cutting a word in half ("lets a solo fo")."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    head = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return head.rstrip(" ,.;:-—–") + "…"
