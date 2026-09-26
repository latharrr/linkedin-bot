"""Generation pipeline (SPEC §5.1) and regenerate (§5.2 regen).

CLI:  python -m app.pipeline "your brief"
      Reads voice/corpus from Supabase, prints both drafts, saves images to ./out/.
      Writes nothing to the DB and sends nothing to Telegram.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import storybank
from app.db import Post
from app.images import hook_of
from app.linkread import brief_with_titles
from app.log import get_logger
from app.research import format_research, research_corpus
from app.services import Services
from app.style_check import (
    HARD_MAX,
    HOOK_MAX,
    airy,
    fixable_issues,
    invented_details,
    post_hook,
)
from app.telegram_ui import send_preview
from app.timeutil import IST, utcnow
from app.verify import (
    MAX_COPIED,
    copied_ratio,
    extract_numbers,
    normalize,
    unverified_numbers,
)
from app.writer import is_own_story, unbold

log = get_logger(__name__)


LINK_NOTE = "\n(The linked article is research source [1]. React to what it actually says, in the author's voice.)"


class PipelineError(RuntimeError):
    pass


@dataclass
class Generated:
    brief: str
    topic: str
    template: str
    research: dict[str, Any]  # {"query", "results", "outline"}
    embedding: list[float]
    drafts: dict[str, str]
    images: dict[str, bytes]


async def load_voice(svc: Services) -> dict[str, Any]:
    voice = await svc.db.get_voice_profile()
    if not voice or not str(voice.get("current_role") or "").strip():
        raise PipelineError("No voice profile with current_role. Run scripts/extract_voice.py first.")
    return voice


STORY_RESEARCH = 2  # outside facts kept when the brief is his own first-person account
OUTLINE_NOTE = (
    "Your previous outline claimed things about him that his brief and facts don't support:\n{claims}\n"
    "The brief is his own account and outranks the research. Build the angle and hooks from what the brief says "
    "happened; outside research may only be context about others."
)


def outline_claims(outline: dict[str, Any]) -> str:
    """The parts of an outline that end up stated as fact in the post."""
    parts = [str(outline.get("insight") or ""), *map(str, outline.get("hooks") or []), str(outline.get("counter_intuitive") or "")]
    return "\n\n".join(p for p in parts if p.strip())


def brief_hook(brief: str) -> str:
    """The brief's first sentence, as a hook of at most HOOK_MAX characters."""
    first = re.split(r"(?<=[.!?])\s+", brief.strip(), maxsplit=1)[0]
    return first if len(first) <= HOOK_MAX else first[: HOOK_MAX - 1].rsplit(" ", 1)[0] + "…"


def with_own_hook(outline: dict[str, Any], brief: str) -> dict[str, Any]:
    """His own story opens best with his own first sentence: offer it as hook 1."""
    if is_own_story(brief):
        own = brief_hook(brief)
        outline["hooks"] = [own, *[h for h in outline.get("hooks") or [] if h != own]][:3]
    return outline


def his_names(facts: list[str]) -> set[str]:
    """His organisations and projects, as named in his facts (PicaPool, ProofMart…)."""
    return {w for f in facts for w in re.findall(r"\b[A-Z][A-Za-z]{2,}\b", f)} - _COMMON_CAPS - {"CLAIM", "RULE", "LPU"}


def invented_lines(text: str, brief: str, facts: list[str], sources: str = "") -> list[tuple[str, list[str]]]:
    return invented_details(text, brief + " " + " ".join(facts), his_names(facts), sources)


async def load_bank(svc: Services) -> dict[str, Any]:
    return storybank.load(await optional(lambda: svc.db.get_setting(storybank.SETTING_KEY), None, "story_bank"))


def with_bank(voice: dict[str, Any], bank: dict[str, Any]) -> dict[str, Any]:
    """His interview answers ride along with the voice, for the writer to draw on."""
    text = storybank.as_text(bank)
    return {**voice, "story_bank": text} if text else voice


def audit_facts(research: dict[str, Any] | None) -> list[str]:
    """What the auditor checks against: his verified background plus his claim rules."""
    research = research or {}
    return [str(f) for f in research.get("background") or []] + [f"CLAIM RULE: {r}" for r in research.get("claim_rules") or []]


def writing_voice(voice: dict[str, Any], brief: str) -> dict[str, Any]:
    """What the writer sees: his curated profile card (who he is, what he does now, what
    he did before) plus tone, style and claim rules, so every post connects to his real
    work. The raw background list stays with the audits and the number check; the card
    is the same facts, written to be quoted, not embellished."""
    if not voice.get("profile"):  # no card yet: fall back to the raw facts for his own stories
        return voice if is_own_story(brief) else {k: v for k, v in voice.items() if k != "background"}
    return {k: v for k, v in voice.items() if k != "background"}


async def grounded_outline(
    svc: Services, brief: str, research: str, recent: str, few_shot: list[str], voice: dict[str, Any],
    facts: list[str] | None = None,
) -> dict[str, Any]:
    """An outline whose angle blends outside research into his story makes every draft
    false, and sentence-level fixes can't repair the frame. Audit the outline's claims;
    regenerate once with the problems named; then drop any hook still flagged."""
    outline = with_own_hook(await svc.writer.outline(brief, research, recent, few_shot, voice), brief)
    facts = facts if facts is not None else [str(f) for f in voice.get("background") or []]
    flagged = await audit_flags(svc.writer, outline_claims(outline), brief, facts, research)
    if not flagged:
        return outline
    log.warning("outline_unsupported", extra={"claims": [f[:100] for f in flagged]})
    note = OUTLINE_NOTE.format(claims="\n".join(f"- {f}" for f in flagged))
    try:
        retry = await svc.writer.outline(brief, research, recent, few_shot, voice, note=note)
    except Exception as exc:
        log.warning("outline_retry_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:160]})
        retry = outline
    still = await audit_flags(svc.writer, outline_claims(retry), brief, facts, research)
    hooks = [h for h in retry.get("hooks") or [] if not any(_present(s, h) or _present(h, s) for s in still)]
    retry["hooks"] = hooks or [brief_hook(brief)]  # his own words are true by definition
    retry = with_own_hook(retry, brief)
    log.info("outline_grounded", extra={"flagged_before": len(flagged), "flagged_after": len(still), "hooks_kept": len(hooks)})
    return retry


async def optional(call: Any, default: Any, what: str) -> Any:
    """An enrichment read (similar topics, style examples): retry once, then carry on
    without it. A network blip there must not cost the whole run (seen live: an
    HTTP/2 stream reset from Supabase)."""
    for attempt in (1, 2):
        try:
            return await call()
        except Exception as exc:
            log.warning("optional_step_failed", extra={"step": what, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"[:160]})
            if attempt == 1:
                await asyncio.sleep(1)
    return default


def recent_topics_text(similar: Any) -> str:
    if similar is None:
        return "none"
    return f'"{similar.topic}" (similarity {similar.similarity:.2f}) — this was covered, find a fresh angle.'


def unverified_for(drafts: dict[str, str], research: dict[str, Any] | None, brief: str) -> dict[str, list[str]]:
    """§5.1 step 8. The brief counts as a source: numbers you supplied aren't invented."""
    corpus = research_corpus(research, brief)
    return {w: unverified_numbers(text, corpus) for w, text in drafts.items()}


MIN_REPAIR_RATIO = 0.6  # a "repair" that deletes >40% of the post is rejected


async def repair_if_needed(writer: Any, text: str, facts: str, corpus: str) -> str:
    """One targeted pass that removes numbers the research doesn't support.
    Kept only if it actually reduced the flags and didn't gut the post; any
    numbers still unsupported are shown as ⚠ in the preview."""
    missing = unverified_numbers(text, corpus)
    if not missing:
        return text
    try:
        repaired = await writer.repair_numbers(text, missing, facts)
    except Exception:
        log.exception("number_repair_failed")
        return text
    still = unverified_numbers(repaired, corpus)
    if len(repaired) < MIN_REPAIR_RATIO * len(text) or len(still) >= len(missing):
        log.warning("number_repair_rejected", extra={"before": missing, "after": still})
        return text
    log.info("number_repair", extra={"removed": [n for n in missing if n not in still], "left": still})
    return repaired


REWRITE_NOTE = (
    "Your previous attempt copied whole sentences from the research. Start over: write the "
    "author's own take on it, in his words. Do not reuse any sentence from the sources."
)


def source_texts(research: dict[str, Any] | None) -> list[str]:
    return [str(r.get("content") or "") for r in (research or {}).get("results") or []]


def copied_for(drafts: dict[str, str], research: dict[str, Any] | None) -> dict[str, bool]:
    sources = source_texts(research)
    return {w: copied_ratio(text, sources) > MAX_COPIED for w, text in drafts.items()}


async def own_words_draft(
    svc: Services, which: str, brief: str, outline: dict[str, Any], facts: str,
    voice: dict[str, Any], few_shot: list[str], research: dict[str, Any],
) -> str:
    """A draft that reproduces the sources (seen with a weaker fallback model copying a
    linked article's opening) is rewritten once. If it still copies, the preview flags it."""
    text = await svc.writer.draft(which, brief, outline, facts, voice, few_shot)
    ratio = copied_ratio(text, source_texts(research))
    if ratio <= MAX_COPIED:
        return text
    log.warning("draft_copied_source", extra={"which": which, "ratio": round(ratio, 2)})
    return await svc.writer.draft(which, brief, outline, facts, voice, few_shot, rewrite_note=REWRITE_NOTE)


async def write_drafts(
    svc: Services,
    brief: str,
    outline: dict[str, Any],
    research: dict[str, Any],
    voice: dict[str, Any],
    few_shot: list[str],
) -> dict[str, str]:
    """Steps 6–7: two candidates (story + contrarian) → ONE final post → de-AI →
    number repair → polish (claim audit) → mobile shape → final audit.
    Returns {"a": the post, "b": ""}: one post per run, stored in draft_a."""
    facts = format_research(research)
    cand_a, cand_b = await asyncio.gather(
        own_words_draft(svc, "a", brief, outline, facts, voice, few_shot, research),
        own_words_draft(svc, "b", brief, outline, facts, voice, few_shot, research),
    )
    post = await final_post(svc, brief, outline, facts, voice, cand_a, cand_b, research)
    post = await svc.writer.deai(post, facts, voice)
    corpus = research_corpus(research, brief)
    if svc.settings.number_repair:  # step 7b
        post = await repair_if_needed(svc.writer, post, facts, corpus)
    facts_about_him = audit_facts(research)
    seen: list[str] = []
    sources = research_corpus({"results": research.get("results") or []})  # the research alone, not his brief
    polished = await polish(svc.writer, post, corpus, brief, facts_about_him, seen=seen, sources=sources)
    if seen and len(polished) < COHERENT_SHARE * len(post):
        # Surgical deletions left a fragment (seen live: a post that opened mid-thought).
        # Rewrite it whole, told exactly what to leave out, then check it once more.
        polished = await rewrite_without(svc, brief, outline, facts, voice, cand_a, cand_b, seen, corpus, facts_about_him, sources) or polished
    post = polished
    post = await fit_shape(svc.writer, post, corpus, [str(h) for h in outline.get("hooks") or []])
    # What the audit still flags is shown under the post ("⚠ check: …"), so his approval
    # is also the review. Stored on the row with the research.
    flagged = await audit_flags(svc.writer, post, brief, facts_about_him, sources)
    flagged += [line for line, _new in invented_lines(post, brief, facts_about_him, sources) if line not in flagged]
    research["claim_flags"] = {"a": flagged[:3], "b": []}
    return {"a": post, "b": ""}


COHERENT_SHARE = 0.7  # polish may remove up to 30% of a post before it's rewritten whole


async def rewrite_without(
    svc: Services, brief: str, outline: dict[str, Any], facts: str, voice: dict[str, Any],
    cand_a: str, cand_b: str, claims: list[str], corpus: str, facts_about_him: list[str], sources: str = "",
) -> str | None:
    note = (
        "Write a complete, coherent post. Do NOT include any of these claims: they aren't supported by "
        "his brief, his background facts or the research:\n" + "\n".join(f"- {c}" for c in dict.fromkeys(claims))
    )
    try:
        post = await svc.writer.final(brief, outline, facts, voice, cand_a, cand_b, note=note)
        post = await svc.writer.deai(post, facts, voice)
        if svc.settings.number_repair:
            post = await repair_if_needed(svc.writer, post, facts, corpus)
        post = await polish(svc.writer, post, corpus, brief, facts_about_him, rounds=1, sources=sources)
    except Exception as exc:
        log.warning("rewrite_without_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return None
    log.info("rewrite_without", extra={"claims": len(claims), "chars": len(post)})
    return post


async def final_post(
    svc: Services, brief: str, outline: dict[str, Any], facts: str, voice: dict[str, Any],
    cand_a: str, cand_b: str, research: dict[str, Any],
) -> str:
    """Step 6b: the final editor merges the best of both candidates into one post. If it
    fails, the story candidate stands in; if it copies a source, it's redone once."""
    try:
        post = await svc.writer.final(brief, outline, facts, voice, cand_a, cand_b)
    except Exception as exc:
        log.warning("final_post_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return cand_a
    if copied_ratio(post, source_texts(research)) > MAX_COPIED:
        log.warning("final_copied_source")
        try:
            post = await svc.writer.final(brief, outline, facts, voice, cand_a, cand_b, note=REWRITE_NOTE)
        except Exception:
            return cand_a
    return post


async def polish(
    writer: Any, text: str, corpus: str, brief: str = "", facts: list[str] | None = None, rounds: int = 2,
    seen: list[str] | None = None, sources: str = "",
) -> str:
    """Step 7d: lint → targeted fix, up to `rounds` times. Mechanical issues (banned
    patterns, stacked statistics) plus a claim audit: sentences about his own experience
    that the brief and his background facts don't support (models blend outside research
    into his story). A fix is kept only if the flagged problems are gone, it added no
    unverifiable number and it kept the post."""
    for round_no in range(1, rounds + 1):
        unsupported = await audit_flags(writer, text, brief, facts or [], sources) if facts is not None else []
        if seen is not None:
            seen.extend(unsupported)
        invented = invented_lines(text, brief, facts or [], sources) if facts is not None else []
        issues = fixable_issues(text, brief, keep_numbers=is_own_story(brief)) + [
            f'Remove or rewrite this sentence so it states only what the brief and facts support: "{u}"' for u in unsupported
        ] + [
            f'This line adds details his facts don\'t contain ({", ".join(new)}); rewrite it to say only what his facts say, or cut it: "{line}"'
            for line, new in invented
        ]
        if not issues:
            return text
        try:
            fixed = await writer.fix(text, issues)
        except Exception as exc:
            log.warning("polish_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
            return text
        # Judge the fix by what was flagged: re-auditing is noisier than the first audit.
        after = (
            len(fixable_issues(fixed, brief, keep_numbers=is_own_story(brief)))
            + sum(1 for u in unsupported if _present(u, fixed))
            + (len(invented_lines(fixed, brief, facts or [], sources)) if facts is not None else 0)
        )
        worse_numbers = len(unverified_numbers(fixed, corpus)) > len(unverified_numbers(text, corpus))
        if after >= len(issues) or worse_numbers or len(fixed) < 0.5 * len(text):
            log.warning("polish_rejected", extra={"round": round_no, "issues": len(issues), "after": after, "worse_numbers": worse_numbers})
            return text
        log.info("polish", extra={"round": round_no, "issues_before": len(issues), "issues_after": after, "unsupported_claims": len(unsupported)})
        text = fixed
        if facts is None and not fixable_issues(text, brief, keep_numbers=is_own_story(brief)):
            return text
    return text


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9%']+", unbold(text).replace("’", "'").lower())


def _present(sentence: str, text: str) -> bool:
    """Is (most of) this flagged sentence still in the text? Tolerates the auditor
    trimming or lightly paraphrasing the sentence it quotes."""
    words, hay = _words(sentence), " ".join(_words(text))
    if len(words) < 5:
        return " ".join(words) in hay
    runs = [" ".join(words[i : i + 5]) for i in range(len(words) - 4)]
    return sum(run in hay for run in runs) >= 0.5 * len(runs)


_ABOUT_HIM = re.compile(r"\b(?:I|I'm|I've|I'd|my|me|we|our|us)\b", re.IGNORECASE)


def sourced_statistic(sentence: str, sources: str) -> bool:
    """A third-person sentence whose every number is in the research (not his brief):
    a correctly sourced statistic, not a claim about him."""
    if not sources or _ABOUT_HIM.search(sentence.replace("’", "'")):
        return False
    numbers = [t for t in extract_numbers(normalize(sentence)) if not t.ignorable]
    return bool(numbers) and not unverified_numbers(sentence, sources)


def could_be_about_him(sentence: str, facts: list[str], sources: str) -> bool:
    """Keep a flag only if the sentence is first person, names one of his organisations
    or projects, or carries a number the research doesn't back (a misstatement). Advice
    and opinions ("Tag every irreversible action…") are dropped as auditor noise."""
    plain = sentence.replace("’", "'")
    if _ABOUT_HIM.search(plain):
        return True
    names = {w for f in facts for w in re.findall(r"\b[A-Z][A-Za-z]{2,}\b", f)} - _COMMON_CAPS
    if any(re.search(rf"\b{re.escape(n)}\b", plain) for n in names):
        return True
    numbers = [t for t in extract_numbers(normalize(plain)) if not t.ignorable]
    return bool(numbers) and bool(unverified_numbers(plain, sources)) if sources else bool(numbers)


_COMMON_CAPS = {"The", "Built", "Shipped", "Earlier", "Summer", "Advanced", "Completed", "Part", "Growth", "Strategic",
                "Third", "Repurposed", "Did", "Through", "Cares", "Training", "Data", "Analytics", "Job", "Simulation",
                "Centre", "Professional", "Enhancement", "Department", "Dept", "Office", "Founder", "Founders", "Intern",
                "Development", "Backend", "Artificial", "Intelligence", "Program", "Challenge", "Young", "Innovator"}


async def audit_flags(writer: Any, text: str, brief: str, facts: list[str], sources: str = "") -> list[str]:
    """Unsupported claims about HIM, minus the auditor's known noise: correctly sourced
    statistics (it doesn't see the research, only his brief and facts)."""
    try:
        flagged = await writer.audit_claims(text, brief, facts)
        return [f for f in flagged if not sourced_statistic(f, sources) and could_be_about_him(f, facts, sources)]
    except Exception as exc:  # an audit that fails must not block delivery
        log.warning("claim_audit_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
        return []


async def fit_shape(writer: Any, text: str, corpus: str, hooks: list[str]) -> str:
    """Step 7c, mobile shape: over HARD_MAX characters → tighten (model); paragraphs over
    ~2 phone lines → split at sentence ends (code, words unchanged); hook past the mobile
    "see more" cut → new hook (model). A model edit is kept only if it did its job,
    didn't gut the post, and added no unverifiable number."""
    for _ in range(2):  # a second try: free fallback models sometimes miss on the first
        if len(text) <= HARD_MAX:
            break
        text = await _tune_checked(writer, text, "short", corpus, hooks, lambda new, before=text: len(new) < len(before))
    text = airy(text)
    if len(post_hook(text)) > HOOK_MAX:
        rest = _after_hook(text)
        text = airy(await _tune_checked(
            writer, text, "hook", corpus, hooks,  # a new hook, with the rest of the post kept
            lambda new: len(post_hook(new)) <= HOOK_MAX and len(_after_hook(new)) >= KEEP_REST * len(rest),
        ))
    return text


KEEP_REST = 0.8  # a hook rewrite must keep at least this much of everything after the hook


def _after_hook(text: str) -> str:
    parts = text.strip().split("\n\n", 1)
    return parts[1] if len(parts) > 1 else ""


async def _tune_checked(writer: Any, text: str, mode: str, corpus: str, hooks: list[str], did_its_job: Any) -> str:
    try:
        edited = await writer.tune(text, mode, hooks)
    except Exception as exc:
        log.warning("fit_shape_failed", extra={"mode": mode, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return text
    worse = len(unverified_numbers(edited, corpus)) > len(unverified_numbers(text, corpus))
    if not edited.strip() or worse or not did_its_job(edited):
        log.warning("fit_shape_rejected", extra={"mode": mode, "before": len(text), "after": len(edited), "worse_numbers": worse})
        return text
    log.info("fit_shape", extra={"mode": mode, "before": len(text), "after": len(edited)})
    return edited


async def make_images(svc: Services, drafts: dict[str, str], outline: dict[str, Any] | None = None) -> dict[str, bytes]:
    """Step 9 (generation half): ONE image per run, shared by both drafts, showing the
    scenario the post is about. A model writes the scene; if that fails, the hook is used."""
    hook = hook_of(drafts["a"])
    scene_call = svc.writer.image_scene(drafts["a"], str((outline or {}).get("insight") or ""))
    scene, headline = await asyncio.gather(scene_call, svc.writer.cover_headline(drafts["a"]), return_exceptions=True)
    if isinstance(scene, BaseException):
        log.warning("image_scene_failed", extra={"error": f"{type(scene).__name__}: {scene}"[:200]})
        scene = [f"A scene that captures: {hook}"]
    if isinstance(headline, BaseException):  # the hook's first words still make a true headline
        log.warning("cover_headline_failed", extra={"error": f"{type(headline).__name__}: {headline}"[:200]})
        headline = " ".join(hook.split()[:8])
    image = await svc.images.generate(scene, card=hook, headline=headline)
    return {"a": image, "b": image}


async def generate(svc: Services, brief: str, on_stage: Any = None) -> Generated:
    """Steps 1–9, without any writes. on_stage(name): a coarse progress callback
    (e.g. for reporting "still researching" to a Telegram chat that asks for status)."""
    stage = on_stage or (lambda _name: None)
    voice = await load_voice(svc)  # step 4 first: fail fast before paid calls
    stored_brief = brief
    links = await svc.links.read_all(brief) if svc.links else {}
    if links:  # "<url> something on this?": the article is source [1] and the subject
        brief = brief_with_titles(brief, links) + LINK_NOTE
    stage("researching")
    embedding = await svc.embedder.embed_query(brief)  # a brief is a query
    similar, research, few_shot_rows = await asyncio.gather(
        optional(lambda: svc.db.find_similar_topic(embedding), None, "similar_topic"),  # step 1
        svc.researcher.research(brief, utcnow().astimezone(IST).year),  # step 2
        optional(lambda: svc.db.match_past_posts(embedding, 3), [], "few_shot"),  # step 3
    )
    if is_own_story(stored_brief) and not links:  # his account is the source; research is context only
        research = {**research, "results": research["results"][:STORY_RESEARCH], "mode": "story"}
    if links:
        known = {p["url"] for p in links.values()}
        research = {**research, "results": list(links.values()) + [r for r in research["results"] if r["url"] not in known]}
    few_shot = [p.text for p in few_shot_rows]
    bank = await load_bank(svc)
    background = [str(f) for f in voice.get("background") or []] + storybank.facts(bank)
    pen = with_bank(writing_voice(voice, stored_brief), bank)
    rules = [f"CLAIM RULE: {r}" for r in voice.get("claim_rules") or []]
    stage("outlining")
    outline = await grounded_outline(  # step 5 (+ audit: the angle must match his own account)
        svc, brief, format_research(research), recent_topics_text(similar), few_shot, pen, background + rules
    )
    # The author's sourced background facts travel with the row so the number check (and any
    # later preview/regen) treats "15+ tools in 8 weeks" as verified, not invented.
    research = {**research, "outline": outline, "background": background, "claim_rules": [str(r) for r in voice.get("claim_rules") or []]}
    stage("writing")
    drafts = await write_drafts(svc, brief, outline, research, pen, few_shot)  # 6–7
    stage("making the image")
    images = await make_images(svc, drafts, outline)  # 9
    log.info("generated", extra={"template": outline["sub_template"], "similar": bool(similar)})
    return Generated(
        brief=stored_brief,
        topic=str(outline.get("insight") or brief)[:300],
        template=outline["sub_template"],
        research=research,
        embedding=embedding,
        drafts=drafts,
        images=images,
    )


async def upload_images(svc: Services, post_id: str, images: dict[str, bytes]) -> dict[str, str]:
    """Step 9 (upload half). Fresh names per run so regen never serves a cached image.
    A shared image is uploaded once and both drafts point at it."""
    if images["a"] == images["b"]:
        url = await upload_with_retry(svc, post_id, "img", images["a"])
        return {"a": url, "b": url}
    urls = await asyncio.gather(*(upload_with_retry(svc, post_id, w, images[w]) for w in ("a", "b")))
    return {"a": urls[0], "b": urls[1]}


UPLOAD_ATTEMPTS = 3


async def upload_with_retry(svc: Services, post_id: str, name: str, data: bytes) -> str:
    """Storage uploads retry on network errors (seen live: a ReadTimeout threw away a
    finished post). Each attempt gets a fresh name: a timed-out upload may have landed,
    and uploads never overwrite."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        path = f"{post_id}/{name}-{uuid.uuid4().hex[:8]}.png"
        try:
            return await svc.db.upload_png(path, data)
        except Exception as exc:
            if attempt == UPLOAD_ATTEMPTS:
                raise
            log.warning("upload_retry", extra={"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"[:160]})
            await asyncio.sleep(2 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def claim_flags_for(post: Post) -> dict[str, list[str]]:
    """Audit flags for AI-written drafts; a draft he edited himself is his own words."""
    flags = (post.research or {}).get("claim_flags") or {}
    return {w: [] if getattr(post, f"edited_{w}", False) else list(flags.get(w) or []) for w in ("a", "b")}


async def preview(svc: Services, post: Post) -> None:
    assert svc.messenger is not None
    drafts = {"a": post.draft("a"), "b": post.draft("b")}
    await send_preview(
        svc.messenger,
        post.chat_id,
        post.id,
        drafts,
        {"a": post.image_a_url, "b": post.image_b_url},
        unverified_for(drafts, post.research, post.brief),
        copied_for(drafts, post.research),
        claim_flags_for(post),
    )


async def run_brief(svc: Services, chat_id: int, brief: str, on_stage: Any = None) -> Post:
    """Full pipeline for a Telegram brief: generate → upload → insert (step 10) → preview (step 11)."""
    gen = await generate(svc, brief, on_stage)
    stage = on_stage or (lambda _name: None)
    stage("saving")
    post_id = str(uuid.uuid4())
    urls = await upload_images(svc, post_id, gen.images)
    post = await svc.db.insert_post(
        {
            "id": post_id,
            "chat_id": chat_id,
            "brief": brief,
            "topic": gen.topic,
            "topic_embedding": gen.embedding,
            "template": gen.template,
            "research": gen.research,
            "draft_a": gen.drafts["a"],
            "draft_b": gen.drafts["b"],
            "image_a_url": urls["a"],
            "image_b_url": urls["b"],
            "status": "awaiting_choice",
        }
    )
    await preview(svc, post)
    return post


async def regenerate(svc: Services, post: Post) -> Post | None:
    """Regen: steps 6–11 on the SAME row. Reuses stored research + outline.
    Returns None if the row left 'awaiting_choice' while we were generating."""
    research = post.research or {}
    outline = research.get("outline")
    if not outline:
        raise PipelineError("This post has no stored outline; send the brief again instead.")
    voice = await load_voice(svc)
    embedding = await svc.db.get_topic_embedding(post.id) or await svc.embedder.embed_query(post.brief)
    few_shot = [p.text for p in await optional(lambda: svc.db.match_past_posts(embedding, 3), [], "few_shot")]
    bank = await load_bank(svc)
    drafts = await write_drafts(svc, post.brief, outline, research, with_bank(writing_voice(voice, post.brief), bank), few_shot)
    urls = await upload_images(svc, post.id, await make_images(svc, drafts, outline))
    updated = await svc.db.update_post(
        post.id,
        {
            "draft_a": drafts["a"],
            "draft_b": drafts["b"],
            "image_a_url": urls["a"],
            "image_b_url": urls["b"],
            "edited_a": False,
            "edited_b": False,
            "chosen": None,  # a pick made before regen referred to the old drafts
            "research": research,  # carries the new drafts' claim flags
        },
        status="awaiting_choice",
    )
    if updated is not None:
        await preview(svc, updated)
    return updated


# ── CLI ──────────────────────────────────────────────────────────────────────
async def _cli(brief: str) -> None:
    from app.config import get_settings
    from app.log import setup_logging
    from app.services import build_services

    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    svc = await build_services(settings)
    gen = await generate(svc, brief)
    if svc.recorder:
        await svc.recorder.drain()
    out = Path("out")
    out.mkdir(exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    warnings = unverified_for(gen.drafts, gen.research, brief)
    print(f"\nTemplate: {gen.template}\nOutline: {json.dumps(gen.research['outline'], ensure_ascii=False, indent=2)}")
    for which in ("a", "b"):
        path = out / f"{stamp}-{which}.png"
        path.write_bytes(gen.images[which])
        print(f"\n{'=' * 70}\nDRAFT {which.upper()}  ({len(gen.drafts[which].split())} words)  image: {path}\n{'=' * 70}")
        print(gen.drafts[which])
        if warnings[which]:
            print(f"\n⚠ unverified numbers: {', '.join(warnings[which])}")
    print("\nSources:")
    for r in gen.research["results"]:
        print(f"  - {r['title']} — {r['url']}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.exit('usage: python -m app.pipeline "your brief"')
    asyncio.run(_cli(sys.argv[1].strip()))
