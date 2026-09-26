"""LLM calls (NVIDIA NIM chat): outline (§8.2), drafts (§8.3), de-AI pass (§8.4),
voice extraction (§8.1). Prompt text lives in app/prompts/*.txt."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from app.apiclient import ChatResult
from app.log import get_logger

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
TEMPLATES = (
    "story_cold_open",
    "story_failure_first",
    "story_dialogue",
    "contrarian_myth_bust",
    "contrarian_teardown",
    "insight_list",
)
TUNE_INSTRUCTIONS = {
    "short": (
        "Make it shorter: about 30% fewer characters and at most 1,200 characters in total. Keep the hook, "
        "the one idea, every number that stays, and the closing question. Cut the weakest lines first."
    ),
    "hook": (
        "Rewrite ONLY the hook (the first 1–2 lines) so it stops the scroll in at most 140 characters: a "
        "specific result or number, a confession, a contrarian claim, or a vivid moment. Keep every other "
        "line exactly as it is."
    ),
    "bold": (
        "Make the take bolder: state the opinion plainly, delete hedges (maybe, might, I think, kind of), "
        "use stronger verbs, and end on a sharper takeaway line before the question. Same facts, same length or shorter."
    ),
}
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|[Mm]y|me|[Ww]e|[Oo]ur)\b")


def is_own_story(brief: str) -> bool:
    """A first-person brief ("my bot copied…", "I built…") is his own account. Eight facts
    on a nearby topic took over such stories in live runs, so the writer gets two, as context."""
    return bool(_FIRST_PERSON.search(brief.replace("’", "'")))


ROLE_A_TAKE = (
    "point of view / informed take: his clear opinion on the topic, backed by the research. "
    "NOT a personal anecdote: the brief isn't his story, so never invent an experience"
)
EDIT_TEMPERATURE = 0.2
ROLE_A = "story / founder-POV"
ROLE_B = "contrarian / insight-list"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
MAX_TOOL_ROUNDS = 3  # one tool call needs 2 model calls (fetch, then answer); a small margin for a second lookup
ToolFn = Callable[[], Awaitable[str]]


class WriterError(RuntimeError):
    pass


class ChatClient(Protocol):
    async def chat(
        self, model: str, messages: list[dict[str, str]], *, temperature: float | None = None, max_tokens: int = 4096
    ) -> ChatResult: ...


@cache
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n")


def render(template: str, **values: Any) -> str:
    """Fill {name} placeholders that appear in `values`; leave JSON braces alone."""

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(values[key]) if key in values else m.group(0)

    return _PLACEHOLDER.sub(sub, template)


def parse_json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise WriterError("model did not return a JSON object")
    try:
        # raw_decode stops at the end of the first object, so a stray trailing "}" or
        # commentary after the JSON (gpt-oss does both) doesn't sink an otherwise good reply.
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise WriterError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WriterError("model JSON is not an object")
    return data


def _salvage_chat_json(text: str) -> dict[str, Any]:
    """Chat output that isn't valid JSON: pull out the reply / tool_call fields if they're
    there, otherwise treat the text as the reply minus stray JSON punctuation. Never raw
    JSON in front of him, and never a draft_topic from a malformed answer."""
    out: dict[str, Any] = {}
    call = re.search(r'"tool_call"\s*:\s*"(\w+)"', text)
    if call:
        out["tool_call"] = call.group(1)
    reply = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if reply:
        try:
            out["reply"] = json.loads(f'"{reply.group(1)}"')
        except json.JSONDecodeError:
            out["reply"] = reply.group(1)
    elif not call:
        out["reply"] = text.strip().strip("{}").strip()
    return out


def unbold(text: str) -> str:
    """Fold 'Mathematical Alphanumeric' lettering (𝐛𝐨𝐥𝐝, 𝘪𝘵𝘢𝘭𝘪𝘤, 𝟑𝟎) to plain characters: screen
    readers and search can't read it, and the author has dropped it. Nothing else is touched."""
    return "".join(unicodedata.normalize("NFKC", ch) if 0x1D400 <= ord(ch) <= 0x1D7FF else ch for ch in text)


def to_bold(text: str) -> str:
    """Plain letters/digits → Unicode sans-serif bold, the only bold LinkedIn shows."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D5D4 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D5EE + ord(ch) - ord("a")))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - ord("0")))
        else:
            out.append(ch)
    return "".join(out)


_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_KEYCAP = re.compile(r"([0-9#*])\ufe0f?\u20e3")  # 1️⃣ → "1."
_MD_HEADING = re.compile(r"^#{1,6}\s+(?=\S)", re.MULTILINE)  # "## Title" (not #hashtags)
_MD_ITALIC = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")  # *word* (not "* bullet")
# "…can't flex (Smartsheet, 2025)." → "…can't flex, per Smartsheet." Sources belong in the sentence.
_BRACKET_CITE = re.compile(
    r"\s*\(((?:[A-Z][\w.&'’/-]*)(?:[ /&][A-Z][\w.&'’/-]*){0,4}),?\s*(?:[A-Z][a-z]{2,8}\.?\s+|Q[1-4]\s+)?(?:19|20)\d\d(?:[-–/]\d{1,2}){0,2}\)"
)
_HASHTAG_LINE = re.compile(r"^(?:#[^\W_][\w]*\s*)+$")
MAX_HASHTAGS = 2  # 0 performs as well as 5+ in 2026; 1-2 niche tags at most (linkedin-skills heuristics)


def _emoji_start(line: str) -> bool:
    ch = line.lstrip()[:1]
    return bool(ch) and (ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF or 0x2B00 <= ord(ch) <= 0x2BFF)


_LEADING_EMOJI = re.compile(r"^\s*[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF][\uFE0F\u200D\U0001F3FB-\U0001F3FF]*\s*")


def _emoji_bullets_to_dashes(text: str) -> str:
    """Two or more consecutive lines starting with an emoji are a list: use "- " bullets.
    A single emoji-led line (a deliberate opener) is left alone."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _emoji_start(lines[j]):
            j += 1
        if j - i >= 2:
            for k in range(i, j):
                lines[k] = "- " + _LEADING_EMOJI.sub("", lines[k], count=1)
        i = max(j, i + 1)
    return "\n".join(lines)


def _cap_hashtags(text: str) -> str:
    """Keep the first MAX_HASHTAGS tags of the closing hashtag line(s); inline tags stay."""
    lines = text.rstrip().split("\n")
    tail: list[str] = []
    while lines and _HASHTAG_LINE.match(lines[-1].strip()):
        tail.insert(0, lines.pop().strip())
    if not tail:
        return text
    tags = list(dict.fromkeys(t for line in tail for t in line.split()))[:MAX_HASHTAGS]
    return "\n".join(lines).rstrip() + "\n\n" + " ".join(tags)


_NUM_RANGE = re.compile(r"(?<=\d)\s?[–—]\s?(?=\d)")  # 2025–2026 → 2025-2026
_SPACED_DASH = re.compile(r"\s+[—–]\s+|\s*—\s*")  # "x — y", "x—y" → "x, y"
_ODD_CHARS = str.maketrans({
    "\u2011": "-", "\u2010": "-", "\u202f": " ", "\u00a0": " ", "\u2009": " ", "\u200a": " ",
    "\u201c": '"', "\u201d": '"',  # curly double quotes: a copy-paste artifact
})
_EMOJI = r"[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF][\uFE0F\u200D\U0001F3FB-\U0001F3FF]*"
_ANY_EMOJI = re.compile(_EMOJI)


def dehumanize_tells(text: str) -> str:
    """Typography that marks text as machine-written: em/en dashes, non-breaking hyphens
    and thin spaces, and emoji. Arrows (→) and "P.S." stay: they're his own habits."""
    text = text.translate(_ODD_CHARS)
    text = _NUM_RANGE.sub("-", text)
    text = _SPACED_DASH.sub(", ", text)
    text = text.replace("–", "-")
    text = re.sub(r",\s*([.,;:!?])", r"\1", text)  # a dash that ended a clause
    text = _ANY_EMOJI.sub("", text)  # 🚨 openers and 🔹 sign-offs read as generated
    return re.sub(r"[ \t]{2,}", " ", "\n".join(line.strip() for line in text.split("\n")))


_URL_IN_POST = re.compile(r"\s*\(?https?://\S+?\)?(?=[\s.,;!?]*(?:\s|$))")


def strip_links(text: str) -> str:
    """Links in the body cut reach sharply; the source belongs in the first comment."""
    return _URL_IN_POST.sub("", text)


def linkedin_format(text: str, fold_bold: bool = False) -> str:
    """LinkedIn renders no markdown: **bold** → Unicode bold (or plain when bold is
    folded), *italics* → plain, keycap-emoji numbering → "1.", markdown headings and
    trailing two-space line breaks removed, runs of blank lines collapsed. Also: a
    bracketed "(Source, 2025)" becomes ", per Source", and the closing hashtag line
    keeps at most 3 tags (models ignore that rule often enough to enforce it here)."""
    text = dehumanize_tells(_emoji_bullets_to_dashes(strip_links(text)))  # emoji bullets become "- " before emoji go
    text = _MD_BOLD.sub(lambda m: (m.group(1) or m.group(2)) if fold_bold else to_bold(m.group(1) or m.group(2)), text)
    text = _KEYCAP.sub(lambda m: f"{m.group(1)}.", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _BRACKET_CITE.sub(lambda m: f", per {m.group(1)}", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _cap_hashtags(re.sub(r"\n{3,}", "\n\n", text))


def clean_post(text: str, fold_bold: bool = False) -> str:
    text = (unbold(text) if fold_bold else text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return linkedin_format(text, fold_bold).strip()


def role_for(which: str, template: str | None, own_story: bool = True) -> str:
    """Candidate A is the story (his own account) or, for a general topic, his informed
    take; B is always contrarian. The outline's sub-template refines A's structure."""
    if which == "a" and not own_story:
        return ROLE_A_TAKE
    base = ROLE_A if which == "a" else ROLE_B
    family = "story_" if which == "a" else ("contrarian_", "insight_list")
    if template and template.startswith(family):
        return f"{base} (structure: {template})"
    return base


def validate_outline(outline: dict[str, Any]) -> dict[str, Any]:
    hooks = [str(h) for h in outline.get("hooks") or [] if str(h).strip()]
    if not outline.get("insight") or not hooks:
        raise WriterError("outline missing insight or hooks")
    template = outline.get("sub_template")
    outline["sub_template"] = template if template in TEMPLATES else TEMPLATES[0]
    outline["hooks"] = hooks[:3]
    return outline


class Writer:
    def __init__(
        self, client: ChatClient, model: str, draft_temperature: float, max_tokens: int = 4096, unbold: bool = False
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = draft_temperature
        self._max_tokens = max_tokens
        self._unbold = unbold  # UNBOLD_DRAFTS: fold 𝐛𝐨𝐥𝐝-Unicode to plain text in every draft

    async def _complete(self, user: str, system: str | None = None, sampled: bool = False) -> str:
        """Candidate drafts are sampled at draft_temperature (variety). Every other call,
        editing and checking included, runs at EDIT_TEMPERATURE: the model default (1.0 for
        gpt-oss) made editors add vivid invented specifics in live runs."""
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        result = await self._client.chat(
            self._model,
            messages,
            temperature=self._temperature if sampled else EDIT_TEMPERATURE,
            max_tokens=self._max_tokens,
        )
        if result.finish_reason == "length":  # OpenAI-schema signal: output was cut off
            raise WriterError(f"{self._model} output was truncated (raise NVIDIA_MAX_TOKENS)")
        if not result.text:
            raise WriterError(f"{self._model} returned no text")
        return result.text

    async def outline(
        self, brief: str, research: str, recent_topics: str, few_shot: list[str], voice: dict[str, Any], note: str = ""
    ) -> dict[str, Any]:
        prompt = render(
            load_prompt("outline"),
            brief=brief,
            research=research,
            recent_topics=recent_topics,
            few_shot_posts="\n---\n".join(unbold(p) for p in few_shot) or "(none yet)",
            voice_profile=json.dumps(voice, ensure_ascii=False),
        )
        if note:
            prompt += f"\n\n{note}"
        return validate_outline(parse_json_object(await self._complete(prompt)))

    async def draft(
        self,
        which: str,
        brief: str,
        outline: dict[str, Any],
        research: str,
        voice: dict[str, Any],
        few_shot: list[str],
        rewrite_note: str = "",
    ) -> str:
        # Examples are for cadence, not glyphs: 𝐛𝐨𝐥𝐝-Unicode costs ~7× the tokens of plain text
        # (measured), which pushed draft calls past Groq's 8k tokens/min. Bold use lives in style_rules.
        posts = ([unbold(p) for p in few_shot] + ["(none)"] * 3)[:3]
        system = render(
            load_prompt("draft_system"),
            current_role=voice["current_role"],
            role=role_for(which, outline.get("sub_template"), is_own_story(brief)),
            voice_profile=json.dumps({k: v for k, v in voice.items() if k not in ("profile", "story_bank")}, ensure_ascii=False),
            post_1=posts[0],
            post_2=posts[1],
            post_3=posts[2],
            copy_playbook=load_prompt("copy_playbook"),
            profile=voice.get("profile") or "(profile not set)",
            story_bank=voice.get("story_bank") or "(empty)",
        )
        user = render(
            load_prompt("draft_user"),
            brief=brief,
            outline_json=json.dumps(outline, ensure_ascii=False),
            research=research,
            rewrite_note=f"\n\n{rewrite_note}" if rewrite_note else "",
        )
        return clean_post(await self._complete(user, system=system, sampled=True), self._unbold)

    async def final(
        self, brief: str, outline: dict[str, Any], research: str, voice: dict[str, Any], draft_a: str, draft_b: str, note: str = ""
    ) -> str:
        """Step 6b: the two candidates → the one post he gets."""
        prompt = render(
            load_prompt("final"),
            copy_playbook=load_prompt("copy_playbook"),
            profile=voice.get("profile") or "(profile not set)",
            story_bank=voice.get("story_bank") or "(empty)",
            voice_profile=json.dumps({k: v for k, v in voice.items() if k not in ("profile", "story_bank")}, ensure_ascii=False),
            brief=brief,
            outline_json=json.dumps(outline, ensure_ascii=False),
            research=research,
            draft_a=draft_a,
            draft_b=draft_b,
            note=f"\n{note}\n" if note else "",
        )
        return clean_post(await self._complete(prompt), self._unbold)  # the final editor: conservative

    async def deai(self, draft: str, research: str, voice: dict[str, Any]) -> str:
        prompt = render(
            load_prompt("deai"),
            copy_playbook=load_prompt("copy_playbook"),
            voice_profile=json.dumps({k: v for k, v in voice.items() if k not in ("profile", "story_bank")}, ensure_ascii=False),
            research=research,
            draft=draft,
        )
        return clean_post(await self._complete(prompt), self._unbold)

    async def repair_numbers(self, draft: str, numbers: list[str], research: str) -> str:
        """Remove exactly the flagged numbers; everything else stays as written."""
        prompt = render(load_prompt("number_repair"), numbers=", ".join(numbers), research=research, draft=draft)
        return clean_post(await self._complete(prompt), self._unbold)

    async def tune(self, draft: str, mode: str, hooks: list[str] | None = None) -> str:
        """One targeted edit (the ✂️ / 🎣 / 🔥 buttons, and automatic tightening)."""
        instruction = TUNE_INSTRUCTIONS[mode]
        if mode == "hook" and hooks:
            instruction += "\nCandidate hooks to pick from or sharpen:\n" + "\n".join(f"- {h}" for h in hooks)
        prompt = render(load_prompt("tune"), instruction=instruction, copy_playbook=load_prompt("copy_playbook"), draft=draft)
        return clean_post(await self._complete(prompt), self._unbold)

    async def audit_claims(self, post: str, brief: str, facts: list[str]) -> list[str]:
        """Step 7d: sentences about his own experience that BRIEF/FACTS don't support."""
        prompt = render(
            load_prompt("claim_audit"), brief=brief, facts="\n".join(f"- {f}" for f in facts) or "(none)", post=unbold(post)
        )
        found = parse_json_object(await self._complete(prompt)).get("unsupported") or []
        return [str(s).strip() for s in found if str(s).strip()][:6]

    async def fix(self, draft: str, issues: list[str]) -> str:
        """Step 7d: repair the specific rule breaks the checker found, nothing else."""
        instruction = "Fix exactly these problems and leave every other line as it is:\n" + "\n".join(f"- {i}" for i in issues)
        prompt = render(load_prompt("tune"), instruction=instruction, copy_playbook=load_prompt("copy_playbook"), draft=draft)
        return clean_post(await self._complete(prompt), self._unbold)

    async def comments(self, post: str, voice: dict[str, Any]) -> list[dict[str, str]]:
        """Two comment options for someone else's post (never posted by the bot)."""
        prompt = render(
            load_prompt("comment"),
            profile=voice.get("profile") or "(profile not set)",
            story_bank=voice.get("story_bank") or "(empty)",
            post=post[:4000],
        )
        found = parse_json_object(await self._complete(prompt)).get("comments") or []
        out = []
        for c in found:
            text = clean_post(str(c.get("text") or ""), self._unbold) if isinstance(c, dict) else ""
            if 60 <= len(text) <= 500:
                out.append({"template": str(c.get("template") or "comment"), "text": text})
        if not out:
            raise WriterError("no usable comment drafts")
        return out[:2]

    async def cover_headline(self, post: str) -> str:
        """3-7 words for the cover image."""
        text = (await self._complete(render(load_prompt("cover_headline"), post=unbold(post)))).strip().splitlines()[0]
        text = dehumanize_tells(text).strip().strip("\"'“”").rstrip(".")
        if not 2 <= len(text.split()) <= 10:
            raise WriterError(f"cover headline has {len(text.split())} words")
        return text

    async def image_scene(self, post: str, insight: str) -> list[str]:
        """Scenes for the cover photo, most specific first: [scene, simpler objects-only
        version]. The image filter refuses some harmless scenes, so there's a second try
        that is still about the post."""
        prompt = render(load_prompt("image_scene"), post=unbold(post), insight=insight or "(none)")
        text = await self._complete(prompt)
        found = re.findall(r"^\s*(?:SCENE|SIMPLE)\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        scenes = [" ".join(s.split()).strip('"')[:900] for s in found] or [" ".join(text.split()).strip('"')[:900]]
        scenes = [s for s in scenes if len(s) >= 20]
        if not scenes:
            raise WriterError("image scene too short")
        return scenes[:2]

    async def converse(
        self,
        history: list[dict[str, str]],
        voice: dict[str, Any] | None,
        run_status: str | None = None,
        tools: dict[str, ToolFn] | None = None,
    ) -> dict[str, Any]:
        """Chat reply → {"reply": str, "draft_topic": str | None}. A reply that isn't the
        requested JSON is still shown as chat, and never starts a draft.

        run_status: session state (is a draft already running, which stage, since when) so
        the model reports status or explains a run is in progress instead of guessing.

        tools: read-only lookups (status, queue, drafts, dashboard, ideas — see bot.py) the
        model can call by name instead of guessing. Never anything that changes state: chat
        only ever proposes a draft_topic, it doesn't start the run itself (bot.py does, and
        only after the Jev gate). Each round trip is one extra model call, so this is capped."""
        voice = voice or {}
        system = render(
            load_prompt("chat_system"),
            background="\n".join(f"- {f}" for f in voice.get("background") or []) or "(not loaded)",
            claim_rules="\n".join(f"- {r}" for r in voice.get("claim_rules") or []) or "(none)",
            run_status=run_status or "No draft is currently running.",
        )
        messages = [{"role": "system", "content": system}, *history]
        for _ in range(MAX_TOOL_ROUNDS):
            result = await self._client.chat(self._model, messages, max_tokens=1024)
            text = (result.text or "").strip()
            if not text:
                raise WriterError(f"{self._model} returned no text")
            try:
                data = parse_json_object(text)
            except WriterError:
                data = _salvage_chat_json(text)
            call = data.get("tool_call")
            tool = tools.get(call) if tools and isinstance(call, str) else None
            if tool is not None:
                log.info("chat_tool_call", extra={"tool": call})
                outcome = await tool()
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"[{call} result]\n{outcome}\n\nNow answer him using this, in the same JSON format."},
                ]
                continue
            topic = data.get("draft_topic")
            reply = str(data.get("reply") or "").strip()
            return {"reply": reply, "draft_topic": topic.strip() if isinstance(topic, str) and topic.strip() else None}
        return {"reply": "That took a couple of lookups too many — ask me again?", "draft_topic": None}

    async def extract_voice(self, posts: list[str]) -> dict[str, Any]:
        prompt = render(load_prompt("voice_extraction"), posts="\n\n---\n\n".join(posts))
        return parse_json_object(await self._complete(prompt))
