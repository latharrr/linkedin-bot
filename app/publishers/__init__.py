"""Publishers: one adapter per platform behind app.publishers.base.Publisher."""

from app.publishers.base import (
    Capabilities,
    Draft,
    MaybeLive,
    NotPublished,
    Publisher,
    PublishError,
    PublishResult,
    Unsupported,
)

PLATFORMS = ("linkedin",)  # grows as adapters land

__all__ = [
    "PLATFORMS", "Capabilities", "Draft", "MaybeLive", "NotPublished", "PublishError", "PublishResult", "Publisher", "Unsupported",
]
