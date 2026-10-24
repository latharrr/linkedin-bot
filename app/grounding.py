"""The grounding gate and the no-repeat rule (deterministic halves).

- Nothing about him ships unless it traces to his brief, profile or verified memory: the
  pipeline blocks a draft whose problems survive one grounded rewrite (Ungrounded), and
  the bot asks him for the real detail instead of delivering it with a warning.
- The profile card is context, never copy: a line sharing a run of words with it is lifted.
- One story per fortnight: a claim, metric or anecdote about him that a post from the last
  REPEAT_WINDOW already used is a repeat, to be reframed or dropped.
- A third-party number nobody can verify is tagged [verify] in the text, never stated as
  fact, and a draft with a [verify] tag can't be scheduled until he resolves it.
"""

from __future__ import annotations

import re
from datetime import timedelta

from app.verify import extract_numbers, normalize, unverified_numbers

REPEAT_WINDOW = timedelta(days=14)
VERIFY_TAG = "[verify]"
LIFT_RUN = 6  # this many words in a row from his profile card = copy-paste
REPEAT_RUN = 5  # this many content words in a row shared with a recent claim = the same story
MIN_CONTENT = 3  # a run counts only with this many non-filler words in it

_WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?[%+]?")
_FILLER = frozenset(
    ["a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "i", "me", "my", "we", "our", "us", "he", "his", "him", "she", "her", "they", "them", "their", "you", "your", "so", "than", "then", "into", "over", "after", "before", "about"]
)
# "[your referral code]", "<link>", "XXXX", "{name}": text the model couldn't fill in.
_PLACEHOLDER = re.compile(
    r"\[(?:your|insert|add|link|code|name|url|referral|todo|tbd|placeholder)\b[^\]\n]{0,40}\]|<(?:your|insert|link|code|name|url)\b[^>\n]{0,30}>|\{[a-z_ ]{2,30}\}|\bX{3,}\b|\b(?:insert|your)\s+(?:referral\s+|promo\s+|invite\s+)?(?:code|link|name|url)\s+here\b",
    re.IGNORECASE,
)


_ME = re.compile(r"\b(?:I|I'm|I've|I'd|I'll)\b|\b(?:my|me|mine|we|our|ours)\b", re.IGNORECASE)
# Capitalised words in his facts that aren't his: months, tools, places, generic nouns.
# What's left names his organisations and projects (PicaPool, ProofMart, UniLyf, Gapl…).
_NOT_HIS = frozenset(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "january", "february", "march", "april", "june", "july", "august", "september", "october", "november", "december", "api", "ast", "cli", "crm", "cse", "cto", "kpi", "rag", "seo", "ssh", "llp", "google", "openai", "gemini", "groq", "linkedin", "telegram", "whatsapp", "android", "react", "docker", "typescript", "ubuntu", "supabase", "socket", "vitest", "playwright", "oauth", "webrtc", "peerjs", "langgraph", "capacitor", "india", "delhi", "punjab", "marketing", "event", "student", "school", "university", "technology", "tech", "building", "connect", "college", "coordinator", "edition", "forward", "internship", "admissions", "organisations", "australia", "the", "built", "shipped", "earlier", "summer", "advanced", "completed", "part", "growth", "strategic", "third", "repurposed", "did", "through", "cares", "training", "data", "analytics", "job", "simulation", "centre", "professional", "enhancement", "department", "dept", "office", "founder", "founders", "intern", "development", "backend", "artificial", "intelligence", "program", "challenge", "young", "innovator", "lpu", "claim", "rule"]
)


def his_own_names(facts: list[str]) -> set[str]:
    """His organisations and projects as named in his facts (PicaPool, UniLyf, ProofMart…)."""
    return {w for f in facts for w in re.findall(r"\b[A-Z][A-Za-z]{2,}\b", f) if w.lower() not in _NOT_HIS}


def words(text: str) -> list[str]:
    """Lower-case words for run matching; a possessive 's is dropped (PicaPool's = PicaPool)."""
    plain = normalize(text).lower().replace("-", " ").replace("’", "'").replace("'s ", " ")
    return [w for w in _WORD.findall(plain) if len(w) > 1 or w.isdigit()]


def _is_about_him(line: str, names: set[str]) -> bool:
    if _ME.search(line.replace("’", "'")):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", line) for n in names if n.lower() not in _NOT_HIS)


def _runs(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    runs = (tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
    return {run for run in runs if sum(w not in _FILLER for w in run) >= MIN_CONTENT}


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def lifted_lines(post: str, profile: list[str]) -> list[str]:
    """Lines that copy a LIFT_RUN-word run from his profile card or background facts."""
    source = set().union(*(_runs(words(p), LIFT_RUN) for p in profile)) if profile else set()
    return [ln for ln in _lines(post) if _runs(words(ln), LIFT_RUN) & source]


def claims_about_him(text: str, names: set[str]) -> list[str]:
    """The lines of a post that say something about him (first person or one of his
    organisations): what the no-repeat log keeps per post."""
    return [ln for ln in _lines(text) if _is_about_him(ln, names)]


def _metrics(line: str) -> set[tuple[object, str]]:
    """His numbers in a line, minus list counts and years ("July 2025" is a date, not a result)."""
    return {
        t.key for t in extract_numbers(normalize(line))
        if not t.ignorable and not (t.kind == "" and t.value == t.value.to_integral_value() and 1990 <= t.value <= 2100 and t.display.isdigit())
    }


def repeated_claims(post: str, recent: list[str], names: set[str]) -> list[str]:
    """Lines about him that reuse a metric or a REPEAT_RUN-word run from a claim a recent
    post already made (the same achievement, number or story)."""
    if not recent:
        return []
    metrics = set().union(*(_metrics(c) for c in recent))
    runs = set().union(*(_runs(words(c), REPEAT_RUN) for c in recent))
    return [ln for ln in claims_about_him(post, names) if (_metrics(ln) & metrics) or (_runs(words(ln), REPEAT_RUN) & runs)]


def placeholder_lines(post: str) -> list[str]:
    """Lines with a blank the writer couldn't fill ("use code [your code]"): only he can."""
    return [ln for ln in _lines(post) if _PLACEHOLDER.search(ln)]


def unverified_about_him(post: str, corpus: str, names: set[str]) -> list[str]:
    """Lines about him carrying a number his brief, background and research don't back."""
    return [ln for ln in claims_about_him(post, names) if unverified_numbers(ln, corpus)]


def mark_unverified(post: str, corpus: str, names: set[str]) -> str:
    """Tag every third-party number the research doesn't back with [verify], right after
    the number, so it can't go out stated as fact. Lines about him are left to the gate."""
    mine = set(claims_about_him(post, names))
    out = []
    for raw in post.split("\n"):
        line = raw
        if raw.strip() and raw.strip() not in mine and VERIFY_TAG not in raw:
            for display in unverified_numbers(raw, corpus):
                m = re.search(rf"(?<![\d.]){re.escape(display)}(?![\d.,]*\d)%?", line)
                if m is not None:
                    line = f"{line[:m.end()]} {VERIFY_TAG}{line[m.end():]}"
                elif VERIFY_TAG not in line:  # written differently ("$2,000" is 2000): tag the line
                    line = f"{line.rstrip()} {VERIFY_TAG}"
        out.append(line)
    return "\n".join(out)


def has_verify_tags(text: str) -> bool:
    return VERIFY_TAG in text


class Problems(dict[str, str]):
    """line → why it can't ship ("unsupported claim about you", "copied from your profile"…)."""

    def add(self, lines: list[str], why: str) -> None:
        for ln in lines:
            self.setdefault(ln, why)

    def text(self, limit: int = 4) -> str:
        return "\n".join(f"• {line[:160]} ({why})" for line, why in list(self.items())[:limit])
