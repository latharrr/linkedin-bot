"""Build voice_profile from your seeded posts (SPEC §8.1). Re-run when your voice shifts.

    python scripts/extract_voice.py

The chat model analyses the source='human' posts in past_posts. current_role is never
guessed: you type it (or confirm the stored one) before anything is saved.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services import build_services  # noqa: E402

MIN_POSTS = 5
ABOUT_FILE = Path("out/about_me.json")  # optional sourced facts + style rules (portfolio site, LinkedIn)
PROFILE_FILE = Path("out/profile_card.md")  # who he is, what he does, his past: shown to the writer
EXTRA_FILE = Path("out/voice_extra.json")  # optional long-form writing (portfolio) for the analysis only


def ask_current_role(existing: str | None, prompt: Callable[[str], str] = input) -> str:
    """Loop until the user gives a non-empty role. Enter keeps an existing one."""
    while True:
        hint = f" [Enter keeps: {existing}]" if existing else ""
        answer = prompt(f"Your current role, one line (e.g. 'Founder, Acme — building AI tutors'){hint}: ").strip()
        if answer:
            return answer
        if existing:
            return existing
        print("current_role is required — every draft prompt uses it.")


def load_background(path: Path = ABOUT_FILE) -> list[str]:
    """True facts about you for story drafts. Every draft prompt carries the voice profile,
    so the model can use real experiences instead of inventing them."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [str(f["fact"] if isinstance(f, dict) else f) for f in data.get("background", []) if f]


def load_list(key: str, path: Path = ABOUT_FILE) -> list[str]:
    """A list from about_me.json: style_rules, voice_notes or claim_rules."""
    if not path.exists():
        return []
    return [str(r) for r in json.loads(path.read_text(encoding="utf-8")).get(key, []) if r]


def load_style_rules(path: Path = ABOUT_FILE) -> list[str]:
    return load_list("style_rules", path)


def load_extra_samples(path: Path = EXTRA_FILE) -> list[str]:
    """Long-form writing by the same author, labelled so the model doesn't mistake it for posts."""
    if not path.exists():
        return []
    samples = json.loads(path.read_text(encoding="utf-8")).get("samples", [])
    return [f"[Long-form writing by the same author — not a LinkedIn post]\n{s}" for s in samples if s]


def finalize_profile(
    extracted: dict[str, Any],
    role: str,
    background: list[str] | None = None,
    style_rules: list[str] | None = None,
    voice_notes: list[str] | None = None,
    claim_rules: list[str] | None = None,
) -> dict[str, Any]:
    """The author's own notes override the model's reading of his posts; claim rules are hard limits."""
    profile = {**extracted, "current_role": role}
    for key, value in (
        ("author_voice_notes", voice_notes),
        ("background", background),
        ("style_rules", style_rules),
        ("claim_rules", claim_rules),
    ):
        if value:
            profile[key] = value
    return profile


async def main() -> None:
    svc = await build_services(get_settings())
    posts = await svc.db.past_post_texts("human")
    if len(posts) < MIN_POSTS:
        sys.exit(f"Only {len(posts)} human posts seeded; seed at least {MIN_POSTS} (15–20 is best) first.")
    extra = load_extra_samples()
    print(f"Analysing {len(posts)} posts + {len(extra)} long-form samples…")
    extracted = await svc.writer.extract_voice(posts + extra)
    if svc.recorder:
        await svc.recorder.drain()
    current = await svc.db.get_voice_profile()
    existing_role = (current or {}).get("current_role") or None
    background = load_background()
    if background:
        print(f"Adding {len(background)} background facts from {ABOUT_FILE}.")
    profile = finalize_profile(
        extracted, ask_current_role(existing_role), background, load_style_rules(), load_list("voice_notes"), load_list("claim_rules")
    )
    if PROFILE_FILE.exists():
        profile["profile"] = PROFILE_FILE.read_text(encoding="utf-8").strip()
        print(f"Adding the profile card from {PROFILE_FILE}.")
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    if input("Save this voice profile? [y/N]: ").strip().lower() != "y":
        sys.exit("Not saved.")
    await svc.db.upsert_voice_profile(profile)
    print("Saved voice_profile.")


if __name__ == "__main__":
    asyncio.run(main())
