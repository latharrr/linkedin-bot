"""Conversation mode for the Telegram bot.

Plain messages get a short chat reply. Asking for a post ("make a post about X",
"draft: X", or saying yes to a suggested topic) starts the draft pipeline. Chat
never posts or schedules anything: drafts still need a human pick AND a time.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

MIN_TOPIC_CHARS = 3  # "RAG" is a fine topic when the request is explicit
HISTORY_TURNS = 12  # messages kept per chat (user + assistant), in memory only

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


@dataclass(frozen=True)
class DraftRequest:
    """topic=None: asked for a post without saying what about (or said "it/that")."""

    topic: str | None


def parse_draft_request(text: str) -> DraftRequest | None:
    """Explicit post requests, matched without a model call. None → treat as chat."""
    t = text.strip()
    if _DRAFT_THAT.match(t):
        return DraftRequest(None)
    m = _REQUEST.match(t) or _SHORT.match(t)
    if not m:
        return None
    topic = m.group("topic").strip().strip(" .!?,:;-—–").strip()
    if topic.lower() in _REFERENCES or len(topic) < MIN_TOPIC_CHARS:
        return DraftRequest(None)
    return DraftRequest(topic)


def wants_bot_to_pick(text: str) -> bool:
    return bool(_YOU_PICK.match(text.strip()))


class ChatMemory:
    """Last few messages per chat so "draft that" and "yes, do it" have context.
    In memory only: a bot restart forgets the conversation, never any post state."""

    def __init__(self, turns: int = HISTORY_TURNS) -> None:
        self._turns = turns
        self._chats: dict[int, deque[dict[str, str]]] = {}

    def add(self, chat_id: int, role: str, content: str) -> None:
        self._chats.setdefault(chat_id, deque(maxlen=self._turns)).append({"role": role, "content": content})

    def history(self, chat_id: int) -> list[dict[str, str]]:
        return list(self._chats.get(chat_id, ()))

    def has_context(self, chat_id: int) -> bool:
        return bool(self._chats.get(chat_id))


def is_decline(text: str) -> bool:
    """"not today", "skip", "no thanks" in reply to "what should the post be about?"."""
    return bool(_DECLINE.match(text.strip()))
