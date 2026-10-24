"""Seed the style corpus with your best human-written posts (SPEC §2 item 9).

    python scripts/seed_past_posts.py [seed_posts.jsonl]

One JSON object per line:
    {"text": "...", "posted_at": "2025-11-03", "likes": 120, "comments": 14}

Rows go in as source='human' with engagement = likes + comments. Safe to re-run:
posts whose exact text is already seeded are skipped.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.corpus import PROMOTION_MIN_ENGAGED_HUMAN  # noqa: E402
from app.services import build_services  # noqa: E402


@dataclass(frozen=True)
class SeedPost:
    text: str
    posted_at: str | None
    engagement: int


def parse_seed_lines(lines: list[str]) -> list[SeedPost]:
    """Validate every line; raise ValueError naming the first bad line."""
    posts: list[SeedPost] = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            text = str(obj["text"]).strip()
            likes, comments = int(obj.get("likes", 0)), int(obj.get("comments", 0))
            posted_at = obj.get("posted_at")
            if posted_at:
                posted_at = date.fromisoformat(str(posted_at)[:10]).isoformat()
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"line {n}: {exc}") from exc
        if not text:
            raise ValueError(f"line {n}: empty text")
        if likes < 0 or comments < 0:
            raise ValueError(f"line {n}: negative engagement")
        posts.append(SeedPost(text, posted_at, likes + comments))
    return posts


async def seed(path: Path) -> None:
    posts = parse_seed_lines(path.read_text(encoding="utf-8").splitlines())
    svc = await build_services(get_settings())
    existing = set(await svc.db.past_post_texts("human"))
    new = [p for p in posts if p.text not in existing]
    if new:
        vectors = await svc.embedder.embed_passages([p.text for p in new])  # batched, rate-limit friendly
        for post, vector in zip(new, vectors, strict=True):
            await svc.db.insert_past_post(post.text, "human", vector, engagement=post.engagement, posted_at=post.posted_at)
    if svc.recorder:
        await svc.recorder.drain()
    engaged = await svc.db.count_engaged_human()
    print(f"Seeded {len(new)} new posts ({len(posts) - len(new)} already present). Engaged human posts: {engaged}.")
    if engaged < PROMOTION_MIN_ENGAGED_HUMAN:
        print(f"Note: the /stats promotion gate stays closed until {PROMOTION_MIN_ENGAGED_HUMAN} human posts have engagement > 0.")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "seed_posts.jsonl")
    if not target.exists():
        sys.exit(f"{target} not found. See the docstring for the format.")
    asyncio.run(seed(target))
