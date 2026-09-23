"""Mechanical check of the draft rules in app/prompts/draft_system.txt and deai.txt.

Hard failures are rules the de-AI pass is told to enforce (banned clichés,
"it's not X, it's Y", >3 hashtags, emoji bullets). Soft warnings are things a
regex can only approximate (length, closing CTA question, question-hook,
"landscape" possibly used as a metaphor). Used by scripts/validate_writer.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BANNED: list[tuple[str, re.Pattern[str]]] = [
    ("in today's fast-paced world", re.compile(r"in today'?s fast[- ]paced world", re.I)),
    ("delve", re.compile(r"\bdelv(?:e|es|ed|ing)\b", re.I)),
    ("game-changer", re.compile(r"\bgame[- ]?changers?\b|\bgame[- ]changing\b", re.I)),
    ("unlock", re.compile(r"\bunlock(?:s|ed|ing)?\b", re.I)),
    ("supercharge", re.compile(r"\bsupercharg(?:e|es|ed|ing)\b", re.I)),
    ("elevate", re.compile(r"\belevat(?:e|es|ed|ing)\b", re.I)),
    ("embark", re.compile(r"\bembark(?:s|ed|ing)?\b", re.I)),
    ("crucial", re.compile(r"\bcrucial(?:ly)?\b", re.I)),
    ("tapestry", re.compile(r"\btapestr(?:y|ies)\b", re.I)),
    ("here's the thing", re.compile(r"\bhere'?s the thing\b", re.I)),
    ("let that sink in", re.compile(r"\blet that sink in\b", re.I)),
    (
        "it's not X, it's Y",
        re.compile(
            r"\b(?:it'?s|it is|this is|that'?s)\s+not\b[^.!?\n]{1,80}?[,;:—–-]\s*(?:it'?s|it is|this is|that'?s)\b"
            r"|\bisn'?t\b[^.!?\n]{1,80}?[.,;:—–]\s*it'?s\b",
            re.I,
        ),
    ),
]
_BANNED += [
    (name, re.compile(pattern, re.I | re.M))
    for name, pattern in (
        ("here's the kicker", r"\bhere'?s the (?:kicker|catch|twist)\b"),
        ("label line (Lesson:/Bottom line:/The result?)", r"^\s*(?:the\s+)?(?:lesson|takeaway|key takeaway|bottom line|the result|result|the payoff|payoff|the fix|the truth|reality check)\s*[:?]"),
        ("let's dive in", r"\blet'?s dive\b|\bdive (?:deep )?into\b"),
        ("in a world where", r"\bin a world where\b"),
        ("seamless", r"\bseamless(?:ly)?\b"),
        ("robust", r"\brobust\b"),
        ("transformative", r"\btransformative\b"),
        ("navigate the", r"\bnavigat(?:e|ing) the\b"),
        ("it's worth noting", r"\bit'?s worth noting\b"),
        ("moreover/furthermore", r"\b(?:moreover|furthermore|additionally)\b"),
        ("unleash", r"\bunleash(?:es|ed|ing)?\b"),
        ("harness the power", r"\bharness(?:ing)? the power\b"),
        ("revolutionize", r"\brevolutioni[sz](?:e|es|ed|ing)\b"),
        ("cutting-edge", r"\bcutting[- ]edge\b"),
        ("at the end of the day", r"\bat the end of the day\b"),
        ("game plan / secret sauce", r"\bsecret sauce\b"),
        # From sergebulaev/linkedin-skills (humanizer audit, 2026): single-hit tells.
        ("here's what/how/why", r"(?:^|[.!?]\s+)here'?s (?:what|how|why)\b"),
        ("stop X, start Y", r"\bstop \w+[^.\n]{0,40}[.,] ?start \w+"),
        ("sincerity announcement", r"^[\s>*-]*(?:let me be (?:honest|real|direct|clear)|i'?ll be (?:honest|real|direct)|honestly\?|real talk|full transparency|not gonna lie|unpopular opinion)\b"),
        ("when it comes to", r"\bwhen it comes to\b"),
        ("in the age of AI", r"\bin the age of ai\b"),
        ("the hard truth", r"\bthe (?:hard|uncomfortable) (?:truth|reality) is\b|\bhere'?s a hard truth\b"),
        ("move the needle", r"\bmove the needle\b|\bneedle[- ]moving\b"),
        ("paradigm shift", r"\bparadigm shift\b"),
        ("filler opener", r"^[\s>*-]*[\"']?(?:in today'?s\b|have you ever (?:wondered|thought)|most people don'?t realize|i'?m (?:excited|thrilled|happy) to (?:share|announce)|(?:honored|humbled) to (?:share|announce))"),
        ("dead closing question", r"\bwhat do you think\?|\bwhat are your thoughts\?|(?:^|\s)thoughts\?\s*$|\bagree or disagree\?|\blet me know in the comments\b|\btag someone\b"),
        ("staccato stack", r"^(?:\w+\. ){2,}\w+\.$"),
        ("one-word paragraph", r"^\w+\.$"),
        ("no X. no Y. just Z", r"\bno \w+\. no \w+\. (?:just|only) \w+"),
        ("all the X. none of the Y", r"\ball (?:of )?the \w+\. none of the \w+"),
        ("pseudo-Socratic", r"\b(?:why|how)\? (?:because|simple)\b"),
    )
]

# Density-scored vocabulary (per paragraph): 1 is English, 3 is a signature.
_DENSITY_VOCAB = re.compile(
    r"\b(?:significant(?:ly)?|crucial(?:ly)?|notably|particularly|comprehensive|insights?|leverag\w*|foster\w*|"
    r"landscape|nuanced|multifaceted|holistic|streamlin\w*|elevat\w*|empower\w*|utiliz\w*|facilitat\w*|"
    r"harness\w*|unlock\w*|ecosystem|fundamentally|essentially|ultimately|quietly|load-bearing|synergy)\b",
    re.I,
)
DENSITY_LIMIT = 3
# The repo says >2 fragments is a tell; his own posts have a median of 2 and 4 or fewer
# in 17 of 20 (measured 2026-09-23), so the limit follows his voice, not the generic one.
FRAGMENT_LIMIT = 4


def dense_paragraphs(text: str) -> list[tuple[str, list[str]]]:
    return [
        (p.strip(), hits)
        for p in text.split("\n\n")
        if len(hits := _DENSITY_VOCAB.findall(p)) >= DENSITY_LIMIT
    ]


def fragment_count(text: str) -> int:
    """Standalone sentences under 4 words. More than 2 per post reads as forced rhythm."""
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    return sum(1 for s in re.split(r"(?<=[.!?])\s+|\n+", body) if 0 < len(s.split()) < 4 and s.strip()[-1:] in ".!")
_HASHTAG = re.compile(r"(?<![\w#])#[^\W_]*[^\W\d_][^\W_]*")
_LANDSCAPE = re.compile(r"\blandscapes?\b", re.I)


# Length targets (research, Sept 2026 + his own top posts: median 1,078 chars, best 924–1,318).
CHAR_MIN, CHAR_MAX = 900, 1400
HARD_MAX = 1600  # above this a draft is tightened automatically
HOOK_MAX = 140  # LinkedIn mobile shows ~140 characters before "see more"
DENSE_PARAGRAPH = 220  # characters; "a very long paragraph" on a phone


def post_hook(text: str) -> str:
    """The first paragraph: what shows above "see more"."""
    return text.strip().split("\n\n", 1)[0].strip()


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[\"“'(\[A-Z0-9])")
_BULLET = re.compile(r"^\s*(?:[-*•→]|\d+[.)])\s")
AIRY_TARGET = 180  # characters per paragraph after splitting: ~2 lines on a phone


def airy(text: str) -> str:
    """Split paragraphs at sentence ends so no block runs past ~2 lines on a phone,
    and so the hook (first paragraph) fits above "see more" when its opening
    sentences allow it. Words are never changed; lists and line-broken paragraphs
    are left alone."""
    out: list[str] = []
    for i, para in enumerate(text.strip().split("\n\n")):
        body = para.strip()
        limit = HOOK_MAX if i == 0 else DENSE_PARAGRAPH
        if len(body) <= limit or "\n" in body or _BULLET.match(body):
            out.append(body)
            continue
        groups: list[str] = []
        for sentence in _SENTENCE_END.split(body):
            cap = HOOK_MAX if i == 0 and len(groups) == 1 else AIRY_TARGET  # the hook group stays ≤140
            if groups and len(groups[-1]) + 1 + len(sentence) <= cap:
                groups[-1] = f"{groups[-1]} {sentence}"
            else:
                groups.append(sentence)
        out.extend(groups)
    return "\n\n".join(p for p in out if p)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF


@dataclass
class StyleReport:
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    words: int = 0

    @property
    def passed(self) -> bool:
        return not self.hard


def check_draft(text: str, min_words: int = 140, max_words: int = 240) -> StyleReport:
    text = text.replace("’", "'")  # curly apostrophes
    report = StyleReport(words=len(text.split()))
    for name, pattern in _BANNED:
        if pattern.search(text):
            report.hard.append(f"cliché: {name}")
    hashtags = _HASHTAG.findall(text)
    if len(hashtags) > 2:
        report.hard.append(f"{len(hashtags)} hashtags (max 2)")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    raw_lines = [ln.strip() for ln in text.splitlines()]
    emoji_run = any(
        a and b and _is_emoji(a[0]) and _is_emoji(b[0]) for a, b in zip(raw_lines, raw_lines[1:], strict=False)
    )  # two emoji-led lines in a row = a list; one emoji-led opener is fine
    if emoji_run or any(re.match(r"[0-9#*]\ufe0f?\u20e3", ln) for ln in lines):
        report.hard.append("emoji used as a bullet")
    if re.search(r"\*\*\S|\S\*\*|^#{1,6}\s", text, re.MULTILINE):
        report.hard.append("markdown formatting (LinkedIn shows it as raw symbols)")
    if re.search(r"\((?:[^()]{0,60}),\s*(?:\w+\s+)?(?:19|20)\d\d(?:[-–/]\d{1,2}){0,2}\)", text):
        report.soft.append("bracketed citation with a date — name the source in the sentence")
    if not min_words <= report.words <= max_words:
        report.soft.append(f"{report.words} words (target {min_words}–{max_words})")
    if not CHAR_MIN <= len(text) <= CHAR_MAX:
        report.soft.append(f"{len(text)} characters (target {CHAR_MIN}–{CHAR_MAX})")
    if len(post_hook(text)) > HOOK_MAX:
        report.soft.append(f"hook is {len(post_hook(text))} characters (max {HOOK_MAX})")
    dense = [p for p in text.split("\n\n") if len(p.strip()) > DENSE_PARAGRAPH]
    if dense:
        report.soft.append(f"{len(dense)} paragraph(s) over {DENSE_PARAGRAPH} characters")
    body = [ln for ln in lines if not _HASHTAG.fullmatch(ln) and not all(_HASHTAG.fullmatch(t) for t in ln.split())]
    if not body or "?" not in body[-1]:
        report.soft.append("does not end with a CTA question")
    if lines and lines[0].endswith("?"):
        report.soft.append("opens with a question")
    if _LANDSCAPE.search(text):
        report.soft.append("'landscape' present (banned as a metaphor)")
    return report


# ── fixable issues (step 7d: lint → one targeted fix) ────────────────────────
MAX_STATS = 2
_STAT = re.compile(r"\d[\d,.]*\s?%|\b\d+(?:\.\d+)?x\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]*[.!?]?")


def stat_count(text: str, own: str = "") -> int:
    """Percentages and multipliers: the statistics readers skim past when stacked.
    Numbers that appear in `own` (the brief: his own data) don't count."""
    mine = {m.group(0).replace(" ", "") for m in _STAT.finditer(own)}
    return len({m.group(0).replace(" ", "") for m in _STAT.finditer(text)} - mine)


def fixable_issues(text: str, own: str = "", keep_numbers: bool = False) -> list[str]:
    """Concrete edit instructions for the rule breaks a model can fix. keep_numbers: the
    brief (`own`) is his own story, so its numbers are the proof and must appear."""
    issues = []
    if keep_numbers and own:
        from app.verify import unverified_numbers

        missing = unverified_numbers(own, text)  # his numbers the draft doesn't carry
        if missing:
            issues.append(f"Use his exact numbers from the brief, where they belong in the story: {', '.join(missing)}. Don't replace them with vague words.")
    plain = text.replace("’", "'")
    for name, pattern in _BANNED:
        for m in pattern.finditer(plain):
            start = plain.rfind("\n", 0, m.start()) + 1
            end = plain.find("\n", m.end())
            line = plain[start : end if end != -1 else len(plain)].strip()
            issues.append(f'Rewrite this line plainly, without the "{name}" pattern: "{line[:200]}"')
    for para, hits in dense_paragraphs(text):
        issues.append(f'This paragraph reads machine-written ({", ".join(hits)}). Say it in plain spoken words: "{para[:200]}"')
    if fragment_count(text) > FRAGMENT_LIMIT:
        issues.append("Too many one-to-three-word sentences; they read as forced rhythm. Keep two or three, merge the rest.")
    if stat_count(text, own) > MAX_STATS:
        issues.append(
            f"The post has {stat_count(text, own)} outside statistics. Keep only the {MAX_STATS} that best support the one idea; "
            "remove the others (and any sentence that only existed to carry one). Keep his own numbers exactly."
        )
    return issues


# ── invented details about HIS work (deterministic) ─────────────────────────
# His claim rules say PicaPool systems are named by problem class only, and the auditor
# model misses embellishment roughly half the time. Implementation vocabulary, acronyms
# and durations in sentences about him must already exist in his facts or brief.
IMPL_TERMS = {
    "utm", "utms", "device", "devices", "timestamp", "timestamps", "table", "tables", "schema", "column", "columns",
    "pipeline", "pipelines", "webhook", "webhooks", "cron", "sql", "query", "queries", "database", "api", "apis",
    "hash", "hashes", "hashed", "fingerprint", "fingerprints", "identifier", "identifiers", "qr", "nfc", "pixel",
    "pixels", "cookie", "cookies", "retention", "cohort", "cohorts", "saas", "vendor", "stack", "etl", "warehouse",
    "bigquery", "postgres", "supabase", "firebase", "mixpanel", "amplitude", "segment", "zapier", "n8n", "python",
    "typescript", "react", "node", "script", "scripts", "model", "models", "embedding", "embeddings", "vector",
    "agent", "agents", "llm", "gpt", "claude", "gemini", "groq", "openai", "latency", "uptime", "throughput",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*")
_DURATION = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|a couple of)\s+(?:day|week|month|hour|minute)s?\b", re.I
)
_ACRONYM = re.compile(r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z]*\b")  # UTM, SaaS, KPI, CRM
_BULLET_LINE = re.compile(r"^\s*(?:→|->|[-*•]|\d+[.)])\s*")
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|my|me|we|our|us)\b", re.I)


def _lines_about_him(text: str, names: set[str]) -> list[str]:
    """Lines in the first person or naming one of his organisations, plus bullets
    under such a line ("I built X:" then "→ step")."""
    out, inherit = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        mine = bool(_FIRST_PERSON.search(line.replace("’", "'"))) or any(n.lower() in line.lower() for n in names)
        if _BULLET_LINE.match(line):
            mine = mine or inherit
        else:
            inherit = mine
        if mine:
            out.append(line)
    return out


# Everyday words that carry no claim; anything else new in a line about him is a detail.
_PLAIN = set(["about", "above", "across", "actually", "after", "again", "against", "also", "always", "among", "another", "anyone", "anything", "around", "away", "back", "because", "been", "before", "being", "below", "between", "both", "bring", "built", "came", "cannot", "could", "couldn't", "didn't", "does", "doesn't", "doing", "done", "down", "during", "each", "either", "else", "enough", "even", "ever", "every", "everyone", "everything", "first", "from", "further", "gave", "getting", "give", "given", "goes", "going", "gone", "good", "great", "have", "having", "here", "hers", "herself", "himself", "how", "however", "into", "it's", "itself", "just", "keep", "kept", "know", "known", "last", "later", "least", "less", "like", "likely", "little", "look", "looked", "looking", "made", "make", "makes", "making", "many", "maybe", "mean", "more", "most", "much", "must", "myself", "need", "needed", "never", "next", "nobody", "none", "nothing", "only", "other", "others", "ours", "ourselves", "over", "own", "really", "right", "same", "seem", "seemed", "should", "show", "showed", "shown", "since", "some", "someone", "something", "soon", "still", "such", "sure", "take", "taken", "tell", "than", "that", "their", "theirs", "them", "themselves", "then", "there", "these", "they", "thing", "things", "think", "this", "those", "though", "through", "thus", "together", "took", "toward", "turned", "under", "until", "upon", "used", "very", "want", "wanted", "wasn't", "week", "weeks", "well", "went", "were", "what", "whatever", "when", "where", "which", "while", "whole", "whose", "will", "with", "within", "without", "won't", "work", "worked", "working", "would", "year", "years", "you'd", "you'll", "you're", "your", "yours", "yourself", "isn't", "aren't", "wasn't", "we're", "we've", "they're", "i'm", "i've", "i'd", "what's", "that's", "there's", "here's", "it'd", "let's", "don't"])
MAX_NEW_WORDS = 4  # new content words in one line about him before it counts as elaboration
MAX_NEW_WORDS_OTHER = 7  # any other line: looser, so opinions, advice and wit pass (live: inventions 8-9, good lines 5-6)


def new_content_words(line: str, allowed: set[str]) -> list[str]:
    words = re.findall(r"[a-z][a-z'-]{3,}", line.lower().replace("’", "'"))
    out = []
    for w in dict.fromkeys(words):
        w = w.removesuffix("'s")
        stem = w.rstrip("s")
        forms = {w, stem, w[:-3] if w.endswith("ing") else w, w[:-2] if w.endswith("ed") else w, w[:-1] if w.endswith("d") else w}
        if w in _PLAIN or forms & allowed or any(p in allowed for p in w.split("-") if len(p) > 3):
            continue
        out.append(w)
    return out


def invented_details(text: str, known: str, names: set[str], research: str = "") -> list[tuple[str, list[str]]]:
    """[(line, new terms)]: lines that add implementation details, acronyms or durations
    that aren't in his facts or brief. Lines about him must match those alone; other
    lines may also use terms from the research."""
    him = set(_lines_about_him(text, names))
    mine, everything = _tokens(known), _tokens(known + " " + research)
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        allowed = mine if line in him else everything
        words = set(_WORD_RE.findall(line.lower()))
        new = sorted(w for w in words & IMPL_TERMS if w.rstrip("s") not in allowed and w not in allowed)
        new += [a for a in _ACRONYM.findall(line) if len(a) <= 6 and a.lower().rstrip("s") not in allowed and a.lower() not in new]
        new += [d.group(0) for d in _DURATION.finditer(line) if d.group(0).lower() not in (known + " " + research).lower()]
        if not new:  # elaboration without technical words ("ran lift tests", "Instagram drove most installs")
            extra = new_content_words(line, everything)
            if len(extra) >= (MAX_NEW_WORDS if line in him else MAX_NEW_WORDS_OTHER):
                new = extra
        if new:
            found.append((line, new))
    return found


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stems = {w[:-3] for w in words if w.endswith("ing") and len(w) > 5} | {w[:-2] for w in words if w.endswith("ed") and len(w) > 4}
    return set(words) | {w.rstrip("s") for w in words} | stems
