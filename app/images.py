"""Images (SPEC §5.1 step 9): FLUX.1-dev on NVIDIA NIM (returns JPEG) →
center-crop 4:5 → 1080x1350 PNG. LinkedIn rejects WebP, so output is always
verified as PNG."""

from __future__ import annotations

import io
import re
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

from app.cover import photo_cover, type_cover
from app.log import get_logger
from app.writer import load_prompt, render, unbold

log = get_logger(__name__)
NEUTRAL_SCENE = (
    "A quiet workspace at dusk: an open notebook with hand-drawn diagrams, a laptop seen from the side, "
    "a coffee mug, soft window light, abstract and calm"
)

TARGET = (1080, 1350)  # 4:5
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


# Words that make NVIDIA's FLUX filter refuse a harmless scene (probed 2026-09-23:
# "a desk in a shared apartment" → CONTENT_FILTERED; the same desk in an office passes).
_PRIVATE_PLACES = [
    (re.compile(r"\b(?:hostel|dorm(?:itory)?)\s+rooms?\b", re.I), "study corner in a library"),
    (re.compile(r"\b(?:shared\s+)?(?:apartment|flat)s?\b", re.I), "coworking space"),
    (re.compile(r"\b(?:hostel|dorm(?:itory)?)s?\b", re.I), "library"),
    (re.compile(r"\bbed\s?rooms?\b", re.I), "office"),
    (re.compile(r"\b(?:in|on)\s+(?:his|her|their|a)\s+bed\b", re.I), "at a desk"),
]


def safe_scene(scene: str) -> str:
    """Move a scene out of private living spaces before it reaches the image model."""
    for pattern, place in _PRIVATE_PLACES:
        scene = pattern.sub(place, scene)
    return scene


class ImageError(RuntimeError):
    pass


def sniff_image_type(data: bytes) -> str | None:
    """'image/png' | 'image/jpeg' | None — by magic bytes, never by filename/header."""
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    return None


def crop_to_4x5_png(raw: bytes) -> bytes:
    """Center-crop any image to 4:5, resize to 1080x1350, encode as PNG."""
    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGB")
        w, h = img.size
        target_ratio = TARGET[0] / TARGET[1]
        if w / h > target_ratio:  # too wide: trim left/right
            new_w = round(h * target_ratio)
            left = (w - new_w) // 2
            box = (left, 0, left + new_w, h)
        else:  # too tall: trim top/bottom
            new_h = round(w / target_ratio)
            top = (h - new_h) // 2
            box = (0, top, w, top + new_h)
        out = img.crop(box).resize(TARGET, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    if sniff_image_type(data) != "image/png":  # pragma: no cover - Pillow guarantees this
        raise ImageError("encoded image is not PNG")
    return data


def hook_of(draft: str) -> str:
    """First non-empty line of a draft — the hook the image illustrates."""
    for line in draft.splitlines():
        if line.strip():
            return line.strip()[:200]
    return draft.strip()[:200]


class ImageClient(Protocol):
    async def flux(self, prompt: str) -> bytes: ...


# Fonts with full punctuation coverage (macOS, then common Linux paths). Pillow's
# built-in font has no em dash or curly quotes; they render as empty boxes.
CARD_FONTS = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
_ASCII_PUNCT = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " "})


def _card_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in CARD_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def card_text(text: str) -> str:
    """Plain printable text for the card: bold folded, typographic punctuation to
    ASCII, anything else no card font can draw (emoji, symbols) dropped."""
    t = unbold(text).translate(_ASCII_PUNCT)
    t = "".join(ch for ch in t if ch.isascii() or ch.isalpha())
    return re.sub(r"[ \t]+", " ", t).strip()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: object, max_width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= max_width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def text_card_png(text: str) -> bytes:
    """Last-resort 1080x1350 card: the hook in plain type on a dark background.
    Keeps a run alive when the image model refuses or is down."""
    img = Image.new("RGB", TARGET, (17, 19, 24))
    draw = ImageDraw.Draw(img)
    font = _card_font(64)
    lines = _wrap(draw, card_text(text), font, TARGET[0] - 140 - 96)[:9]
    y = (TARGET[1] - len(lines) * 84) // 2
    draw.rectangle((96, y - 48, 108, y + len(lines) * 84 - 24), fill=(124, 92, 255))
    for line in lines:
        draw.text((140, y), line, font=font, fill=(236, 238, 242))
        y += 84
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class ImageMaker:
    def __init__(self, client: ImageClient) -> None:
        self._client = client

    async def _flux_png(self, scene: str) -> bytes:
        raw = await self._client.flux(render(load_prompt("image"), scene=scene.replace('"', "'")))
        if sniff_image_type(raw) is None:
            raise ImageError("FLUX returned bytes that are not PNG/JPEG")
        return crop_to_4x5_png(raw)

    async def photo(self, scene: str | list[str]) -> bytes | None:
        """Scene photo → simpler scene → neutral photo → None (then a type cover)."""
        scenes = [scene] if isinstance(scene, str) else list(scene)
        tries = [(f"scene{i + 1}", safe_scene(unbold(s))) for i, s in enumerate(scenes)] + [("neutral", NEUTRAL_SCENE)]
        for attempt, prompt_scene in tries:
            try:
                return await self._flux_png(prompt_scene)
            except Exception as exc:
                log.warning("image_fallback", extra={"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return None

    async def generate(self, scene: str | list[str], card: str | None = None, headline: str | None = None) -> bytes:
        """The post image: a designed cover. With a headline, a photo cover (or, if the
        image model refuses or is down, a type cover). Without one, the bare photo."""
        photo = await self.photo(scene)
        if headline:
            return photo_cover(photo, headline) if photo else type_cover(headline)
        first = scene if isinstance(scene, str) else (scene[0] if scene else "")
        return photo if photo else text_card_png(card or unbold(first))
