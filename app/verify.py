"""Number verification (SPEC §5.1 step 8) — mechanical, not prompt-based.

Every number in a draft must also appear in the research. Both texts are
normalized first so "$2B" matches "2 billion" and "47%" matches "47 percent".
Magnitude units are folded into the numeric value, so "$2B", "2 billion" and
"2,000,000,000" are the same stat, and "₹5 crore" equals "50 million".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

CURRENCY_MARK = "¤"  # replaces $ / ₹ so "$5" is still known to be money (not a list count)

_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")
_CURRENCY = re.compile(r"(?:US\$|\$|₹|\bRs\.?\s?|\bINR\s?)(?=\s?\d)")
_CURRENCY_GAP = re.compile(re.escape(CURRENCY_MARK) + r"\s+(?=\d)")
_WORD_UNITS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\s?\b(?:per\s?cent|percent|pct)\b", re.I), "%"),
    (re.compile(r"\b(?:billions?|bn)\b", re.I), "B"),
    (re.compile(r"\b(?:millions?|mn)\b", re.I), "M"),
    (re.compile(r"\bthousands?\b", re.I), "K"),
    (re.compile(r"\bcrores?\b", re.I), "Cr"),
    (re.compile(r"\b(?:lakhs?|lacs?)\b", re.I), "L"),
]
_NUMBER = re.compile(
    r"(?<![\w.])(?P<cur>" + re.escape(CURRENCY_MARK) + r")?"
    r"(?P<num>\d+(?:\.\d+)?)\s?(?P<unit>%|cr|[bmklx])?(?!\w)",
    re.I,
)
_MULTIPLIER = {
    "b": Decimal(10) ** 9,
    "m": Decimal(10) ** 6,
    "k": Decimal(10) ** 3,
    "cr": Decimal(10) ** 7,
    "l": Decimal(10) ** 5,
}


@dataclass(frozen=True)
class NumberToken:
    value: Decimal
    kind: str  # "" plain/magnitude, "%" percent, "x" multiplier
    display: str
    ignorable: bool  # bare integer 1–10 with no unit: a list count, not a stat

    @property
    def key(self) -> tuple[Decimal, str]:
        return (self.value, self.kind)


def normalize(text: str) -> str:
    """Strip currency symbols and digit-group commas; map unit words to symbols."""
    text = _DIGIT_COMMA.sub("", text)
    text = _CURRENCY.sub(CURRENCY_MARK, text)
    text = _CURRENCY_GAP.sub(CURRENCY_MARK, text)
    for pattern, symbol in _WORD_UNITS:
        text = pattern.sub(symbol, text)
    return text


def extract_numbers(normalized: str) -> list[NumberToken]:
    tokens: list[NumberToken] = []
    for m in _NUMBER.finditer(normalized):
        raw_num, unit = m.group("num"), (m.group("unit") or "").lower()
        try:
            value = Decimal(raw_num)
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
        kind = unit if unit in ("%", "x") else ""
        if unit in _MULTIPLIER:
            value *= _MULTIPLIER[unit]
        ignorable = (
            not unit
            and not m.group("cur")
            and "." not in raw_num
            and 1 <= int(raw_num) <= 10
        )
        display = m.group(0).replace(CURRENCY_MARK, "").replace(" ", "")
        tokens.append(NumberToken(value.normalize(), kind, display, ignorable))
    return tokens


def _same_value_other_form(keys: set[tuple[Decimal, str]]) -> set[tuple[Decimal, str]]:
    """A fraction and its percentage are the same number: a source's 0.92 verifies a
    draft's 92%, and 92% verifies 0.92 (models restate one as the other)."""
    extra = set()
    for value, kind in keys:
        if kind == "" and 0 < value < 1:
            extra.add(((value * 100).normalize(), "%"))
        elif kind == "%" and 0 < value <= 100:
            extra.add(((value / 100).normalize(), ""))
    return extra


def unverified_numbers(draft: str, research_text: str) -> list[str]:
    """Numbers in `draft` that do not appear (after normalization) in `research_text`."""
    known = {t.key for t in extract_numbers(normalize(research_text))}
    known |= _same_value_other_form(known)
    missing: list[str] = []
    seen: set[tuple[Decimal, str]] = set()
    for token in extract_numbers(normalize(draft)):
        if token.ignorable or token.key in known or token.key in seen:
            continue
        seen.add(token.key)
        missing.append(token.display)
    return missing


def warning_line(missing: list[str]) -> str:
    return f"⚠ unverified numbers: {', '.join(missing)}" if missing else ""


# ── verbatim copying ─────────────────────────────────────────────────────────
SHINGLE = 8  # an 8-word run shared with a source is copying, not coincidence
MAX_COPIED = 0.2  # share of a draft's 8-word runs found verbatim in the sources
_WORD = re.compile(r"[a-z0-9']+")


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'"))
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def copied_ratio(draft: str, sources: list[str]) -> float:
    """Share of the draft's 8-word runs that appear word-for-word in any source."""
    mine = _shingles(draft)
    if not mine:
        return 0.0
    theirs: set[tuple[str, ...]] = set()
    for src in sources:
        theirs |= _shingles(src)
    return len(mine & theirs) / len(mine)
