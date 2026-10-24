"""Platform-neutral publishing: one Publisher per platform (LinkedIn today; Telegram,
Instagram, Facebook Pages and Threads next).

The dispatcher only ever talks to this interface. Every publisher raises one of two
errors, and the difference is the whole safety model:
  NotPublished: definitely not live (a 4xx, or the request never left) → safe to retry.
  MaybeLive:    could be live (timeout, 5xx, a broken 2xx) → never retried; a human
                checks the platform and taps Live ✓ / Not live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

Media = Literal["text", "image", "video", "carousel"]


class PublishError(RuntimeError):
    """Base for every publisher failure."""


class NotPublished(PublishError):
    """Nothing is live: safe to retry."""


class MaybeLive(PublishError):
    """It may be live: never retried automatically."""


class Unsupported(NotPublished):
    """The platform can't take this draft (e.g. a text-only post to Instagram)."""


@dataclass(frozen=True)
class Capabilities:
    media: frozenset[Media]
    native_scheduling: bool
    max_text_len: int
    text_only: bool = True  # False: every post needs media (Instagram)


@dataclass(frozen=True)
class Draft:
    """What gets published: the approved text plus its media, already downloaded."""

    text: str
    image: bytes | None = None
    image_type: str | None = None  # "image/png" | "image/jpeg"
    title: str = ""  # a short label some platforms show with media (LinkedIn: the image title)


@dataclass(frozen=True)
class PublishResult:
    platform_post_id: str
    url: str
    data: dict[str, Any] = field(default_factory=dict)  # platform extras, stored in posts.platform_data


class Publisher(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    def check(self, draft: Draft) -> str | None:
        """Why this platform can't take `draft`, in words for him; None if it can."""
        caps = self.capabilities
        if len(draft.text) > caps.max_text_len:
            return f"{self.name} allows {caps.max_text_len} characters; this post has {len(draft.text)}."
        if not caps.text_only and draft.image is None:
            return f"{self.name} needs an image or video; this post has none."
        if draft.image is not None and "image" not in caps.media:
            return f"{self.name} can't post images."
        return None

    @abstractmethod
    async def publish(self, draft: Draft) -> PublishResult:
        """Publish now. Raises NotPublished or MaybeLive."""

    async def schedule(self, draft: Draft, when: datetime) -> PublishResult:
        """Hand the post to the platform's own scheduler. Only for native_scheduling
        platforms; for the rest the dispatcher holds the post and calls publish() on time."""
        raise Unsupported(f"{self.name} has no native scheduling: the dispatcher publishes at the time")

    @abstractmethod
    async def delete(self, platform_post_id: str) -> None:
        """Remove a published post."""
