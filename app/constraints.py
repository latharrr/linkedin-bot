"""Constraints he states in chat ("keep my personal story out for now", "no politics").

They hold until he revokes them, across messages and restarts (settings table), and every
proposal is checked against them: the chat reply, each pitched idea, and the draft itself
(the grounding gate reads the rules the bot adds to the brief).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.grounding import his_own_names

SETTING_KEY = "session_constraints:{chat_id}"

# Added to a brief so the writer and the gate know. Plain words, no first person, so a
# brief carrying it isn't mistaken for his own account (writer.is_own_story).
NO_STORY_NOTE = "\n\n(No personal story for this one: write it as his informed take, with no anecdote, metric or claim about his own history.)"
AVOID_NOTE = "\n\n(Stay away from these topics entirely: {topics}.)"
_AVOID_IN_BRIEF = re.compile(r"\(Stay away from these topics entirely: ([^)]*)\.\)")

_STORY = r"(?:story|stories|anecdotes?|experiences?|life|work\s+stor(?:y|ies))"
_SET_PERSONAL = re.compile(
    rf"\b(?:keep|leave)\s+(?:my\s+)?(?:personal|own)\s+{_STORY}\s+out\b"
    rf"|\b(?:no|without|skip|avoid|drop)\s+(?:more\s+)?(?:my\s+)?(?:personal|own)\s+{_STORY}"
    r"|\bdon'?t\s+(?:use|mention|bring\s+up|include)\s+my\s+(?:personal\s+|own\s+)?(?:story|stories|life|work|experiences?)\b"
    r"|\bnothing\s+personal\b",
    re.IGNORECASE,
)
_CLEAR_PERSONAL = re.compile(
    r"\b(?:personal|my\s+own|my)\s+(?:stor(?:y|ies)|anecdotes?|experiences?)\s+(?:are|is)\s+(?:fine|ok(?:ay)?|allowed|back|good)\b"
    r"|\b(?:you\s+can|feel\s+free\s+to|go\s+ahead\s+and|ok(?:ay)?\s+to)\s+use\s+my\s+(?:personal\s+|own\s+)?(?:story|stories|experiences?|work)\b"
    r"|\bbring\s+back\s+(?:my\s+)?(?:personal\s+)?stor(?:y|ies)\b",
    re.IGNORECASE,
)
_SENSITIVE = r"politics|political|religion|religious|crypto|web3|elections?|layoffs?|wars?|controvers(?:y|ial)|gossip|funding\s+news"
_SET_AVOID = re.compile(
    rf"\b(?:no|avoid|skip|stay\s+away\s+from|keep\s+away\s+from|nothing\s+(?:on|about)|don'?t\s+(?:do|write\s+about|pitch|suggest))\s+"
    rf"(?:any\s+|more\s+)?({_SENSITIVE}|[a-z][a-z0-9-]{{2,20}}(?=\s+(?:posts?|topics?|content|stuff|takes?|ideas?)\b))",
    re.IGNORECASE,
)
_CLEAR_AVOID = re.compile(r"\b([a-z][a-z0-9-]{2,20})\s+(?:is|are)\s+(?:fine|ok(?:ay)?|allowed)(?:\s+(?:again|now))?\b", re.IGNORECASE)
_CLEAR_ALL = re.compile(r"\b(?:clear|drop|forget|reset)\s+(?:all\s+)?(?:the\s+|my\s+)?(?:constraints|rules|restrictions)\b", re.IGNORECASE)

# A line that tells a story of his: his organisations, his past-tense first person, "my team".
_PERSONAL_LINE = re.compile(
    r"\b(?:I|we)\s+(?:\w+ly\s+)?(?:\w+ed|built|ran|made|spent|was|were|had|took|led|lost|got|went|saw|joined|wrote|shipped|broke|paid|told|"
    r"learn[td]|found|bought|left|began|started|won|sold|grew|cut|set)\b|\bmy\s+(?:team|startup|company|job|internship|boss|family|parents|"
    r"laptop|first|last|own|work|story|stories|project|projects|attribution|tools?|bot|system)\b|\bat\s+my\b",
    re.IGNORECASE,
)
_THIRD_PERSON = re.compile(r"\b(?:his|he|he's|him|himself)\b", re.IGNORECASE)  # ideas are pitched about "him"
# The chat model talks to him ("your PicaPool attribution system"), and says "I" about itself.
_SECOND_PERSON = re.compile(
    r"\byour\s+(?:own\s+)?(?:work|story|stories|experiences?|team|startup|internships?|attribution|tools?|system|projects?|"
    r"journey|time\s+at|days\s+at|role|job|family|laptop|growth\s+hacks?)\b",
    re.IGNORECASE,
)


def stem(term: str) -> str:
    """"politics" / "political" → "polit": what a line is matched on."""
    term = re.sub(r"\s+(?:posts?|topics?|content|stuff|takes?|ideas?)$", "", term.lower().strip())
    return re.sub(r"(?:ics|ical|ious|ion|s)$", "", term) if len(term) > 5 else term


@dataclass
class Constraints:
    no_personal: bool = False
    avoid: list[str] = field(default_factory=list)  # topics, as he said them ("politics")

    def __bool__(self) -> bool:
        return self.no_personal or bool(self.avoid)

    def to_json(self) -> str:
        return json.dumps({"no_personal": self.no_personal, "avoid": self.avoid})

    @classmethod
    def from_json(cls, raw: str | None) -> Constraints:
        try:
            data = json.loads(raw or "{}")
            return cls(bool(data.get("no_personal")), [str(t) for t in data.get("avoid") or []])
        except (ValueError, AttributeError):
            return cls()

    def update(self, text: str) -> list[str]:
        """Apply what he just said; returns what changed, in words for the acknowledgement."""
        changed: list[str] = []
        if _CLEAR_ALL.search(text) and self:
            self.no_personal, self.avoid = False, []
            return ["cleared every constraint"]
        if _CLEAR_PERSONAL.search(text) and self.no_personal:
            self.no_personal = False
            changed.append("personal stories are back in")
        elif _SET_PERSONAL.search(text) and not self.no_personal:
            self.no_personal = True
            changed.append("no personal stories")
        for m in _CLEAR_AVOID.finditer(text):
            gone = [t for t in self.avoid if stem(t) == stem(m.group(1))]
            for t in gone:
                self.avoid.remove(t)
                changed.append(f"{t} is back in")
        for m in _SET_AVOID.finditer(text):
            term = re.sub(r"\s+", " ", m.group(1).lower())
            if term in ("personal", "own", "more", "any") or any(stem(t) == stem(term) for t in self.avoid):
                continue
            self.avoid.append(re.sub(r"\s+(?:posts?|topics?|content|stuff|takes?|ideas?)$", "", term))
            changed.append(f"no {self.avoid[-1]}")
        return changed

    def describe(self) -> str:
        parts = (["no personal stories (nothing about his own work, history or life)"] if self.no_personal else []) + [f"no {t}" for t in self.avoid]
        return "; ".join(parts)

    def prompt_text(self) -> str:
        if not self:
            return ""
        return (f"Constraints he set (they hold until he lifts them; never propose, pitch or draft anything that breaks one, "
                f"and don't suggest his own work stories while personal stories are off): {self.describe()}.")

    def breaks(self, text: str, facts: list[str], who: str = "draft") -> str | None:
        """Why this text breaks a constraint, or None. who: "draft" (his first person),
        "idea" (pitched about "him": any his/he is his story) or "chat" (said to him)."""
        for t in self.avoid:
            if re.search(rf"\b{re.escape(stem(t))}", text, re.IGNORECASE):
                return f"it's about {t}"
        if self.no_personal:
            names = his_own_names(facts)
            if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
                return "it uses your own work"
            personal = {"draft": _PERSONAL_LINE, "idea": _THIRD_PERSON, "chat": _SECOND_PERSON}[who]
            if personal.search(text) or (who == "idea" and _PERSONAL_LINE.search(text)):
                return "it's a personal story"
        return None

    def brief(self, brief: str) -> str:
        """The brief with his standing rules attached, for the writer and the gate."""
        if self.no_personal and NO_STORY_NOTE not in brief:
            brief += NO_STORY_NOTE
        if self.avoid and not _AVOID_IN_BRIEF.search(brief):
            brief += AVOID_NOTE.format(topics=", ".join(self.avoid))
        return brief


def rules_in_brief(brief: str) -> Constraints:
    """The rules the bot attached to a brief (Constraints.brief), read back by the gate."""
    m = _AVOID_IN_BRIEF.search(brief)
    return Constraints(NO_STORY_NOTE in brief, [t.strip() for t in m.group(1).split(",")] if m else [])


def personal_lines(text: str, facts: list[str]) -> list[str]:
    """Lines of a draft that tell a story of his (for a no-personal-story brief)."""
    c = Constraints(no_personal=True)
    return [ln.strip() for ln in text.splitlines() if ln.strip() and c.breaks(ln, facts)]
