"""Dispatcher (SPEC §6) — cron every 5 min. Publishes rows a human queued.

    python dispatcher.py            publish due posts
    python dispatcher.py --dry-run  log the exact payloads; never calls LinkedIn,
                                    never claims or changes a row, sends nothing

Only rows with status='queued' are ever touched, and only the bot's time
buttons (after a draft pick) put a row there — both human gates are upstream.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from telegram import Bot

from app import corpus
from app.config import Settings, get_settings
from app.db import Database, Post
from app.images import hook_of, sniff_image_type
from app.linkedin import (
    ImageUploadError,
    LinkedInClient,
    LinkedInError,
    PostMaybeLive,
    PostRejected,
    build_post_payload,
    post_url_from_restli_id,
)
from app.log import get_logger, register_secret, setup_logging
from app.services import Embedder, build_nvidia
from app.telegram_ui import (
    Messenger,
    TelegramMessenger,
    stuck_keyboard,
    telegram_request,
)
from app.timeutil import utcnow
from app.usage import UsageRecorder

log = get_logger("dispatcher")

STUCK_AFTER = timedelta(minutes=15)
MAX_ATTEMPTS = 2
BATCH = 5
DRY_RUN_AUTHOR = "urn:li:person:DRY_RUN"
DRY_RUN_IMAGE = "urn:li:image:DRY_RUN"
MAYBE_LIVE = "⚠ May already be live — check LinkedIn before touching it: {brief}"
TOKEN_MISSING = "LinkedIn token missing — run scripts/linkedin_auth.py"

FetchImage = Callable[[str], Awaitable[bytes]]


HEARTBEAT_KEY = "dispatcher_last_run"  # read by the bot's /status board
AFTER_POST_TIPS = (
    "The next 60-90 minutes decide its reach: reply to every comment. "
    "Fix typos only; hold bigger edits for 3 hours (a big early edit resets distribution)."
)


@dataclass
class Dispatcher:
    db: Database
    embedder: corpus.Embeds
    messenger: Messenger
    chat_id: int
    linkedin: LinkedInClient | None  # None = no token stored (or dry run)
    fetch_image: FetchImage
    dry_run: bool = False
    report: Counter[str] = field(default_factory=Counter)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def alert(self, text: str, post: Post | None = None) -> None:
        await self.messenger.send_text(self.chat_id, text, stuck_keyboard(post.id) if post else None)

    # step 1 ----------------------------------------------------------------------
    async def resolve_author(self) -> str:
        urn = await self.db.get_setting("linkedin_author_urn")
        if urn:
            return urn
        if self.dry_run:
            return DRY_RUN_AUTHOR
        if self.linkedin is None:
            raise LinkedInError(TOKEN_MISSING)
        urn = f"urn:li:person:{await self.linkedin.userinfo_sub()}"
        await self.db.set_setting("linkedin_author_urn", urn)
        return urn

    # step 2 ----------------------------------------------------------------------
    async def check_stuck(self, now: datetime) -> None:
        for post in await self.db.stuck_posts(now - STUCK_AFTER):
            if self.dry_run:
                log.info("dry_run_stuck_row", extra={"post_id": post.id})
                continue
            if await self.db.mark_alerted(post.id, now):  # once per row, even with overlapping runs
                await self.alert(MAYBE_LIVE.format(brief=post.brief[:200]), post)
                self.report["stuck_alerted"] += 1

    # steps 3–6 -------------------------------------------------------------------
    async def run(self, now: datetime) -> Counter[str]:
        author: str | None = None
        author_error = TOKEN_MISSING
        try:
            author = await self.resolve_author()
        except LinkedInError as exc:
            author_error = str(exc)
            log.error("author_urn_unavailable", extra={"error": author_error})
        await self.check_stuck(now)
        for post in await self.db.due_posts(now, BATCH):
            if self.dry_run:
                await self.dry_publish(post, author or DRY_RUN_AUTHOR)
                continue
            if not await self.db.claim_post(post.id, now):
                self.report["lost_claim"] += 1  # another worker has it
                continue
            if author is None or self.linkedin is None:
                await self.fail_retryable(post, author_error)
                continue
            try:
                await self.publish(post, author, now)
            except Exception:
                # Unknown state: leave it in 'posting'. The stuck check alerts in 15 min.
                log.exception("publish_crashed", extra={"post_id": post.id})
                self.report["crashed"] += 1
        if not self.dry_run:  # heartbeat for /status: "publisher last ran N min ago"
            try:
                await self.db.set_setting(HEARTBEAT_KEY, now.isoformat())
            except Exception:
                log.warning("heartbeat_failed")
        return self.report

    async def download(self, url: str | None) -> tuple[bytes, str]:
        """LinkedIn won't pull from a URL: fetch the bytes and verify PNG/JPEG."""
        if not url:
            raise ImageUploadError("post has no image URL")
        try:
            data = await self.fetch_image(url)
        except Exception as exc:
            raise ImageUploadError(f"image download: {type(exc).__name__}: {exc}") from exc
        content_type = sniff_image_type(data)
        if content_type is None:
            raise ImageUploadError("image is not PNG/JPEG (LinkedIn rejects WebP)")
        return data, content_type

    async def publish(self, post: Post, author: str, now: datetime) -> None:
        assert self.linkedin is not None
        which = post.chosen or "a"
        text = post.draft(which)
        if not text.strip():  # never publish an empty post (e.g. draft B chosen on a one-post row)
            return await self.fail_retryable(post, f"draft {which} is empty — nothing to post")
        image_urn: str | None = None
        try:
            data, content_type = await self.download(post.image_url(which))
            upload_url, image_urn = await self.linkedin.init_upload(author)
            await self.linkedin.put_image(upload_url, data, content_type)
            restli_id = await self.linkedin.create_post(build_post_payload(author, text, image_urn, hook_of(text)))
            url = post_url_from_restli_id(restli_id)
        except (ImageUploadError, PostRejected) as exc:
            return await self.fail_retryable(post, str(exc))
        except PostMaybeLive as exc:
            return await self.maybe_live(post, str(exc), now)
        except ValueError as exc:  # 2xx but an id we can't turn into a URL: likely live
            return await self.maybe_live(post, f"posted but {exc}", now)
        await self.succeed(post, url, image_urn, now)

    async def dry_publish(self, post: Post, author: str) -> None:
        which = post.chosen or "a"
        text = post.draft(which)
        try:
            data, content_type = await self.download(post.image_url(which))
            image = {"bytes": len(data), "content_type": content_type}
        except ImageUploadError as exc:
            image = {"error": str(exc)}
        payload = build_post_payload(author, text, DRY_RUN_IMAGE, hook_of(text))
        self.payloads.append(payload)
        self.report["dry_run"] += 1
        log.info("dry_run_payload", extra={"post_id": post.id, "chosen": which, "image": image, "payload": payload})

    # outcomes --------------------------------------------------------------------
    async def succeed(self, post: Post, url: str, image_urn: str | None, now: datetime) -> None:
        fields = {"status": "posted", "posted_at": now, "image_urn": image_urn, "post_url": url, "error": None}
        try:
            updated = await self.db.update_post(post.id, fields, status="posting")
        except Exception:
            log.exception("posted_but_db_update_failed", extra={"post_id": post.id, "post_url": url})
            await self.alert(f"Posted ✓ {url}\n\nBut the database update failed, so the row is still 'posting'. "
                             "When the may-be-live alert arrives, tap Live ✓ and paste this URL.")
            return
        self.report["posted"] += 1
        log.info("posted", extra={"post_id": post.id, "post_url": url})
        try:
            await corpus.admit_on_post(self.db, self.embedder, updated or post.model_copy(update=fields))
        except Exception:
            log.exception("corpus_admission_failed", extra={"post_id": post.id})
        await self.alert(f"Posted ✓ {url}\n\n{AFTER_POST_TIPS}")

    async def fail_retryable(self, post: Post, error: str) -> None:
        """Definitely not posted: back to queued, or failed after MAX_ATTEMPTS."""
        attempts = post.retry_count + 1
        status = "queued" if attempts < MAX_ATTEMPTS else "failed"
        fields = {"retry_count": attempts, "error": error[:1000], "status": status, "claimed_at": None}
        await self.db.update_post(post.id, fields, status="posting")
        self.report["retry" if status == "queued" else "failed"] += 1
        log.warning("post_failed", extra={"post_id": post.id, "attempt": attempts, "status": status, "error": error})
        next_step = "Retrying on the next run." if status == "queued" else "Marked failed. Nothing was posted."
        await self.alert(f"LinkedIn post failed (attempt {attempts}/{MAX_ATTEMPTS}): {error}\n\n{next_step}")

    async def maybe_live(self, post: Post, error: str, now: datetime) -> None:
        """Timeout/5xx on create: leave in 'posting', alert once, never retry."""
        await self.db.update_post(post.id, {"alerted_at": now, "error": error[:1000]}, status="posting")
        self.report["maybe_live"] += 1
        log.error("post_maybe_live", extra={"post_id": post.id, "error": error})
        await self.alert(MAYBE_LIVE.format(brief=post.brief[:200]) + f"\n\n({error})", post)


# ── entry point ──────────────────────────────────────────────────────────────
class LogMessenger:
    """Dry-run messenger: logs instead of sending."""

    async def send_text(self, chat_id: int, text: str, keyboard: Any = None) -> None:
        log.info("dry_run_telegram", extra={"text": text})

    async def send_photo(self, chat_id: int, photo_url: str) -> None:
        log.info("dry_run_telegram_photo", extra={"url": photo_url})

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        return None


async def amain(settings: Settings, dry_run: bool) -> Counter[str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.http_timeout)) as http:

        async def fetch_image(url: str) -> bytes:
            resp = await http.get(url, follow_redirects=True)
            resp.raise_for_status()
            return resp.content

        db = await Database.connect(
            settings.supabase_url, settings.supabase_service_key.get_secret_value(), settings.supabase_bucket, settings.http_timeout
        )
        nvidia = build_nvidia(settings, http)
        recorder = UsageRecorder(db)
        nvidia.recorder = recorder
        embedder = Embedder(nvidia, settings.nvidia_embed_model)
        token = await db.get_setting("linkedin_access_token")
        register_secret(token)
        linkedin = LinkedInClient(http, token, settings.linkedin_api_version) if token and not dry_run else None

        async def run(messenger: Messenger) -> Counter[str]:
            d = Dispatcher(db, embedder, messenger, settings.my_chat_id, linkedin, fetch_image, dry_run)
            return await d.run(utcnow())

        if dry_run:
            report = await run(LogMessenger())
        else:
            async with Bot(settings.telegram_bot_token.get_secret_value(), request=telegram_request(settings)) as bot:
                report = await run(TelegramMessenger(bot))
        await recorder.drain()
    log.info("dispatch_done", extra={"dry_run": dry_run, **report})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="log payloads; never call LinkedIn or change rows")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    asyncio.run(amain(settings, args.dry_run))


if __name__ == "__main__":
    main()
