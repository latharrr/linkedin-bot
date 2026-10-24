"""LinkedIn behind the Publisher interface: the reference implementation.

Same calls, in the same order, as before the extraction: initializeUpload → image PUT →
POST /rest/posts, and the x-restli-id header becomes the feed URL. The REST client and
the little-text escaping stay in app/linkedin.py."""

from __future__ import annotations

from typing import Any, ClassVar

from app.linkedin import (
    ImageUploadError,
    LinkedInClient,
    LinkedInError,
    build_post_payload,
    post_url_from_restli_id,
)
from app.publishers.base import Capabilities, Draft, MaybeLive, Publisher, PublishResult

AUTHOR_KEY = "linkedin_author_urn"  # settings row
TOKEN_MISSING = "LinkedIn token missing — run scripts/linkedin_auth.py"


class LinkedInPublisher(Publisher):
    name: ClassVar[str] = "linkedin"
    capabilities: ClassVar[Capabilities] = Capabilities(
        media=frozenset({"text", "image"}),  # the bot posts one image per post
        native_scheduling=False,  # the dispatcher holds queued rows and publishes on time
        max_text_len=3000,
    )

    def __init__(self, client: LinkedInClient | None, author: str) -> None:
        self._client = client
        self._author = author

    @staticmethod
    async def resolve_author(db: Any, client: LinkedInClient | None) -> str:
        """The member URN posts go out as: cached in settings, else looked up once."""
        urn = await db.get_setting(AUTHOR_KEY)
        if urn:
            return urn
        if client is None:
            raise LinkedInError(TOKEN_MISSING)
        urn = f"urn:li:person:{await client.userinfo_sub()}"
        await db.set_setting(AUTHOR_KEY, urn)
        return urn

    def payload(self, draft: Draft, image_urn: str) -> dict[str, Any]:
        return build_post_payload(self._author, draft.text, image_urn, draft.title)

    async def publish(self, draft: Draft) -> PublishResult:
        """ImageUploadError / PostRejected are NotPublished, PostMaybeLive is MaybeLive
        (see app/linkedin.py). A 2xx whose id isn't a URN is treated as maybe live."""
        if self._client is None:
            raise LinkedInError(TOKEN_MISSING)
        if draft.image is None or draft.image_type is None:
            raise ImageUploadError("post has no image URL")
        upload_url, image_urn = await self._client.init_upload(self._author)
        await self._client.put_image(upload_url, draft.image, draft.image_type)
        restli_id = await self._client.create_post(self.payload(draft, image_urn))
        try:
            url = post_url_from_restli_id(restli_id)
        except ValueError as exc:
            raise MaybeLive(f"posted but {exc}") from exc
        return PublishResult(restli_id, url, {"image_urn": image_urn})

    async def delete(self, platform_post_id: str) -> None:
        if self._client is None:
            raise LinkedInError(TOKEN_MISSING)
        await self._client.delete_post(platform_post_id)
