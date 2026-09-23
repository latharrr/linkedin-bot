"""Story bank: the true raw material posts are made of, collected by interviewing him.

Idea taken from sergebulaev/linkedin-skills (linkedin-interviewer + story-bank.md):
the profile says who he is; the bank holds what he has to say, in his own words,
with numbers, dates, turning points, scars and positions. The writer draws on it
instead of inventing, and the audits treat it as verified.

Rules kept from that design: one question at a time; press a vague answer once,
then move on; a declined question is never asked again; answers are stored verbatim.
Stored in the settings table (never in a git-tracked file).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SETTING_KEY = "story_bank"


@dataclass(frozen=True)
class Question:
    id: str
    section: str
    text: str


SECTIONS = {
    "receipts": "Receipts (numbers he can state without checking)",
    "turning_points": "Turning points (what he believed before, what he believes now)",
    "scars": "Scars (what broke, the real cost, what he does differently)",
    "positions": "Positions (opinions he'd defend, and what holding them costs)",
    "stories": "Stories he already tells out loud",
    "moments": "Recent moments",
    "names": "Names he may and may not use",
    "off_limits": "Off limits (never post about)",
}

# One moment per question, never a category (the repo's question-bank rule). His roles
# and projects are already in the profile card, so the interview starts where it's thin.
QUESTIONS: tuple[Question, ...] = (
    Question("moment_now", "moments", "What have you been working on lately that you can't stop thinking about?"),
    Question("receipt_picapool", "receipts", "At PicaPool, what's one number you'd stand behind? What was it before, what was it after, and over how long?"),
    Question("receipt_time", "receipts", "What did one of your tools save, in hours or money? A rough number is fine, just say it's rough."),
    Question("receipt_unilyf", "receipts", "At UniLyf, what's one number from the pre-launch work you're proud of, and what did it change?"),
    Question("turn_belief", "turning_points", "What did you believe a year ago that you no longer believe? What changed your mind?"),
    Question("turn_plan", "turning_points", "When did a plan change under you, and what did you do about it?"),
    Question("scar_cost", "scars", "What's the most expensive mistake you've made so far, in time or money? What did it cost?"),
    Question("scar_check", "scars", "What do you check now that you never used to check?"),
    Question("position_contrarian", "positions", "What do you think is true that most people around you disagree with?"),
    Question("position_cost", "positions", "What does holding that view cost you? Who would argue with you?"),
    Question("stories_told", "stories", "Which story do you end up telling in person again and again? Give it in two lines."),
    Question("moment_week", "moments", "What did you actually spend most of last week doing?"),
    Question("names_free", "names", "Which people, companies or tools are you free to name in public posts?"),
    Question("names_never", "names", "Anyone or anything that must never be named, or that needs asking first?"),
    Question("off_limits", "off_limits", "Any subjects that should never appear in your posts, however well they'd do?"),
)
BY_ID = {q.id: q for q in QUESTIONS}

# Sections where a number or date is the whole point: a vague answer gets one follow-up.
NEEDS_SPECIFICS = {"receipts", "scars", "moments"}
_VAGUE = re.compile(r"\b(recently|a while|a lot|lots|many|some|significant(ly)?|improved|better|big|huge|a bit|few)\b", re.I)
_SPECIFIC = re.compile(r"\d|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b", re.I)
# Whole-reply matches only: "Never trust downloads" is an answer, not a decline.
_SKIP = re.compile(r"^\s*(skip|pass|next|dunno|i don'?t know|don'?t know|no idea)\W*$", re.I)
_NEVER = re.compile(r"^\s*(never|never ask( me)?( that| this)?( again)?|rather not( say)?|i'?d rather not( say)?|no comment|private)\W*$", re.I)
_STOP = re.compile(r"^\s*(stop|done|enough|that'?s all|finish|end|done for now)\W*$", re.I)

PRESS = "Put a number or a month on that, even a rough one (say it's rough). Or send \"skip\"."


def is_vague(question: Question, answer: str) -> bool:
    return question.section in NEEDS_SPECIFICS and not _SPECIFIC.search(answer) and bool(_VAGUE.search(answer) or len(answer.split()) < 8)


def classify(answer: str) -> str:
    """'stop' | 'never' | 'skip' | 'answer'."""
    if _STOP.match(answer):
        return "stop"
    if _NEVER.match(answer):
        return "never"
    if _SKIP.match(answer):
        return "skip"
    return "answer"


def empty_bank() -> dict[str, Any]:
    return {"entries": [], "declined": [], "sessions": 0, "updated": None}


def load(raw: str | None) -> dict[str, Any]:
    try:
        bank = json.loads(raw) if raw else empty_bank()
    except ValueError:
        bank = empty_bank()
    return {**empty_bank(), **bank}


def dump(bank: dict[str, Any]) -> str:
    return json.dumps(bank, ensure_ascii=False)


def answered_ids(bank: dict[str, Any]) -> set[str]:
    return {e["qid"] for e in bank["entries"]}


def next_question(bank: dict[str, Any], skipped: set[str] | None = None) -> Question | None:
    """The first question not answered, not declined, and not skipped this session."""
    done = answered_ids(bank) | set(bank["declined"]) | (skipped or set())
    return next((q for q in QUESTIONS if q.id not in done), None)


def add_answer(bank: dict[str, Any], q: Question, answer: str, now: datetime) -> dict[str, Any]:
    entries = [e for e in bank["entries"] if e["qid"] != q.id or e.get("follow_up")]
    entries.append({"qid": q.id, "section": q.section, "question": q.text, "answer": answer.strip(), "at": now.isoformat()})
    return {**bank, "entries": entries, "updated": now.isoformat()}


def add_follow_up(bank: dict[str, Any], q: Question, answer: str, now: datetime) -> dict[str, Any]:
    entry = {"qid": q.id, "section": q.section, "question": PRESS, "answer": answer.strip(), "at": now.isoformat(), "follow_up": True}
    return {**bank, "entries": [*bank["entries"], entry], "updated": now.isoformat()}


def decline(bank: dict[str, Any], q: Question) -> dict[str, Any]:
    return {**bank, "declined": sorted({*bank["declined"], q.id})}


def as_text(bank: dict[str, Any]) -> str:
    """For the writer and the audits: his answers, verbatim, by section."""
    lines: list[str] = []
    for key, title in SECTIONS.items():
        entries = [e for e in bank["entries"] if e["section"] == key]
        if not entries:
            continue
        lines.append(f"{title}:")
        for e in entries:
            lines.append(f"- {e['answer']}" if e.get("follow_up") else f"- Q: {e['question']} A: {e['answer']}")
    return "\n".join(lines)


def facts(bank: dict[str, Any]) -> list[str]:
    """Answers as verified facts for the claim audit and the invented-detail check."""
    return [f"His own words ({SECTIONS[e['section']].split(' (')[0]}): {e['answer']}" for e in bank["entries"]]


def summary(bank: dict[str, Any]) -> str:
    counts = {key: sum(1 for e in bank["entries"] if e["section"] == key and not e.get("follow_up")) for key in SECTIONS}
    filled = [f"{SECTIONS[k].split(' (')[0]}: {n}" for k, n in counts.items() if n]
    thin = [SECTIONS[k].split(" (")[0] for k, n in counts.items() if not n]
    parts = ["📚 Story bank", "", "Filled: " + (", ".join(filled) if filled else "nothing yet")]
    if thin:
        parts.append("Still thin: " + ", ".join(thin))
    left = sum(1 for q in QUESTIONS if q.id not in answered_ids(bank) | set(bank["declined"]))
    parts.append(f"Questions left: {left}")
    return "\n".join(parts)
