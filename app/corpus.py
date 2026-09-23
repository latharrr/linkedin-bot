"""Style-corpus admission (SPEC §6 step 5, §6.4) and /stats promotion (§5.3).

The corpus never learns from raw AI output. An AI draft gets in only if
(a) you edited the chosen draft (admitted when it posts), or
(b) it later beats median human engagement, once ≥5 engaged human rows exist.
"""

from __future__ import annotations

import statistics
from typing import Literal, Protocol

from app.db import Database, Post
from app.log import get_logger
from app.timeutil import IST

log = get_logger(__name__)

PROMOTION_MIN_ENGAGED_HUMAN = 5
StatsOutcome = Literal["updated", "promoted", "gate_closed", "below_median"]


class Embeds(Protocol):
    async def embed_passage(self, text: str) -> list[float]: ...


def should_admit_on_post(post: Post) -> bool:
    return (post.chosen == "a" and post.edited_a) or (post.chosen == "b" and post.edited_b)


def promotion_gate_open(engaged_human_count: int) -> bool:
    return engaged_human_count >= PROMOTION_MIN_ENGAGED_HUMAN


def beats_median(engagement: int, human_engagements: list[int]) -> bool:
    """Strictly greater than the median of engaged human posts."""
    return bool(human_engagements) and engagement > statistics.median(human_engagements)


def _posted_date(post: Post) -> str | None:
    return post.posted_at.astimezone(IST).date().isoformat() if post.posted_at else None


async def _insert_ai_row(db: Database, embedder: Embeds, post: Post, engagement: int) -> str:
    text = post.draft(post.chosen or "a")
    past_id = await db.insert_past_post(
        text, "ai", await embedder.embed_passage(text), engagement=engagement, posted_at=_posted_date(post)
    )
    await db.update_post(post.id, {"past_post_id": past_id})
    return past_id


async def admit_on_post(db: Database, embedder: Embeds, post: Post) -> str | None:
    """Called right after a post goes live. Only edited chosen drafts get in."""
    if post.past_post_id or not should_admit_on_post(post):
        return None
    past_id = await _insert_ai_row(db, embedder, post, engagement=0)
    log.info("corpus_admitted_edited", extra={"post_id": post.id, "past_post_id": past_id})
    return past_id


async def record_stats(db: Database, embedder: Embeds, post: Post, likes: int, comments: int) -> StatsOutcome:
    """§5.3: update engagement if already in the corpus; otherwise run the promotion gate."""
    engagement = likes + comments
    if post.past_post_id:
        await db.update_past_post_engagement(post.past_post_id, engagement)
        return "updated"
    if not promotion_gate_open(await db.count_engaged_human()):
        return "gate_closed"
    if not beats_median(engagement, await db.engaged_human_engagements()):
        return "below_median"
    past_id = await _insert_ai_row(db, embedder, post, engagement)
    log.info("corpus_promoted", extra={"post_id": post.id, "past_post_id": past_id, "engagement": engagement})
    return "promoted"
