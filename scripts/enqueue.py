"""Enqueue an already-approved post for the dispatcher to publish (SPEC §6).

    python scripts/enqueue.py TEXT_FILE IMAGE_FILE "2026-10-25 09:00" ["topic"]

Use when a draft was approved outside Telegram (e.g. reviewed on WhatsApp):
uploads the image to the post-images bucket, inserts one row with status
'queued', chosen='a', scheduled_at in IST, and prints the row id plus the
SHA-256 of the stored text so the enqueue can be verified end to end.
The dispatcher's normal exactly-once claim publishes it; nothing here posts
directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services import build_services  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


async def enqueue(text_file: str, image_file: str, when: str, topic: str) -> None:
    text = Path(text_file).read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("text file is empty")
    data = Path(image_file).read_bytes()
    if not data.startswith(b"\x89PNG") and not data.startswith(b"\xff\xd8"):
        raise SystemExit("image must be PNG or JPEG (LinkedIn rejects WebP)")
    try:
        at = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    except ValueError:
        raise SystemExit("time must be 'YYYY-MM-DD HH:MM' (IST)") from None
    svc = await build_services(get_settings())
    try:
        url = await svc.db.upload_png(f"manual/{at:%Y-%m-%d-%H%M}-{Path(image_file).name}", data)
        post = await svc.db.insert_post({
            "chat_id": svc.settings.my_chat_id,
            "brief": f"manual enqueue (approved outside Telegram): {topic}",
            "topic": topic,
            "draft_a": text,
            "image_a_url": url,
            "chosen": "a",
            "scheduled_at": at,
            "status": "queued",
        })
        stored = await svc.db.get_post(post.id)
        assert stored is not None
        digest = hashlib.sha256((stored.draft_a + "\n").encode()).hexdigest()
        print(f"queued id={post.id}")
        print(f"scheduled_at={stored.scheduled_at.isoformat()}")
        print(f"text_sha256={digest} len={len(stored.draft_a)}")
        print(f"image={stored.image_a_url}")
    finally:
        if svc.recorder:
            await svc.recorder.drain()


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        raise SystemExit(__doc__)
    asyncio.run(enqueue(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) == 5 else ""))
