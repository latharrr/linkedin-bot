"""One-time: rebuild every embedding with the NVIDIA model after
scripts/migrate_embeddings_2048.sql cleared them.

    python scripts/reembed_corpus.py            # re-embed everything that is NULL
    python scripts/reembed_corpus.py --dry-run  # just count what's missing

- past_posts.text   → passage vectors (few-shot retrieval targets)
- posts.brief       → query vectors   (posts.topic_embedding, used for topic dedup)

Idempotent and resumable: it only fills NULL vectors, so re-run it after any
interruption. Paced to stay under NVIDIA's free-tier limit (~40 req/min/model).
A 20-post corpus is 2–3 requests; don't use this for large bulk backfills.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services import Embedder, build_services  # noqa: E402

PAUSE_SECONDS = 2.0  # ≤30 req/min, comfortably under the ~40/min free-tier cap


async def reembed(
    db, embedder: Embedder, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, batch: int = Embedder.BATCH
) -> dict[str, int]:
    counts = {"past_posts": 0, "posts": 0}
    while rows := await db.past_posts_missing_embedding(batch):
        vectors = await embedder.embed_passages([text for _, text in rows])
        for (row_id, _), vector in zip(rows, vectors, strict=True):
            await db.set_past_post_embedding(row_id, vector)
        counts["past_posts"] += len(rows)
        await sleep(PAUSE_SECONDS)
    while rows := await db.posts_missing_topic_embedding(batch):
        vectors = await embedder.embed_queries([brief for _, brief in rows])
        for (row_id, _), vector in zip(rows, vectors, strict=True):
            await db.set_topic_embedding(row_id, vector)
        counts["posts"] += len(rows)
        await sleep(PAUSE_SECONDS)
    return counts


async def main(dry_run: bool) -> None:
    svc = await build_services(get_settings())
    if dry_run:
        past = await svc.db.past_posts_missing_embedding(10_000)
        posts = await svc.db.posts_missing_topic_embedding(10_000)
        print(f"Missing embeddings: {len(past)} past_posts, {len(posts)} posts. Nothing written.")
        return
    counts = await reembed(svc.db, svc.embedder)
    if svc.recorder:
        await svc.recorder.drain()
    print(f"Re-embedded {counts['past_posts']} past_posts and {counts['posts']} posts with {svc.settings.nvidia_embed_model}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
