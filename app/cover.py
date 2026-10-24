"""The post image as a designed cover, not a raw AI render.

- Photo cover: a documentary-style photo (FLUX) with film grain and slightly muted
  colour, a dark fade over the lower part, a bold 3-7 word headline and his name.
- Type cover (when there's no photo): a paper-toned card with a heavy black headline
  and an accent bar, like a hand-designed carousel cover.

Both are 1080x1350 (4:5 portrait, the size LinkedIn's mobile feed favours).
"""

from __future__ import annotations

import io
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

W, H = 1080, 1350
MARGIN = 84
AUTHOR = "Deepanshu Lathar"

# (path, face index): heaviest first; macOS, then common Linux paths
HEAVY = (
    ("/System/Library/Fonts/Avenir Next.ttc", 8),  # Heavy
    ("/System/Library/Fonts/HelveticaNeue.ttc", 1),  # Bold
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", 0),
)
MEDIUM = (
    ("/System/Library/Fonts/Avenir Next.ttc", 5),  # Medium
    ("/System/Library/Fonts/HelveticaNeue.ttc", 10),
    ("/System/Library/Fonts/Helvetica.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", 0),
)
PAPER, INK, ACCENT, MUTED = (243, 239, 230), (22, 22, 22), (228, 87, 46), (110, 106, 98)


@lru_cache(maxsize=64)
def font(faces: tuple[tuple[str, int], ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path, index in faces:
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def clean_headline(text: str, max_words: int = 14) -> str:
    """One line of plain words: no dashes, quotes, emoji, hashtags or trailing full stop."""
    t = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", text)
    t = re.sub(r"\s*[—–]\s*", ", ", t).replace("‑", "-")
    t = re.sub(r"#\w+", "", t).strip().strip("\"'“”‘’ ").rstrip(".")
    words = t.split()
    return " ".join(words) if len(words) <= max_words else " ".join(words[:max_words]) + "…"  # never silently cut


def _wrap(draw: ImageDraw.ImageDraw, text: str, f: object, width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=f) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def fit_headline(draw: ImageDraw.ImageDraw, text: str, width: int, start: int = 112, max_lines: int = 4) -> tuple[object, list[str], int]:
    """Largest size (down to 56px) at which the headline fits in max_lines."""
    size = start
    while True:
        f = font(HEAVY, size)
        lines = _wrap(draw, text, f, width)
        fits = len(lines) <= max_lines and all(draw.textlength(ln, font=f) <= width for ln in lines)
        if fits or size <= 56:
            return f, lines[:max_lines], size
        size -= 6


def _grain(img: Image.Image, strength: float = 0.06) -> Image.Image:
    """Film grain + slightly muted colour: takes the plastic sheen off generated photos."""
    img = ImageEnhance.Color(img).enhance(0.86)
    img = ImageEnhance.Contrast(img).enhance(1.04)
    noise = Image.effect_noise(img.size, 64).convert("RGB")
    return Image.blend(img, noise, strength)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def photo_cover(photo_png: bytes, headline: str, author: str = AUTHOR) -> bytes:
    with Image.open(io.BytesIO(photo_png)) as src:
        img = _grain(src.convert("RGB").resize((W, H), Image.Resampling.LANCZOS))
    # Dark fade over the lower 55% so white type reads on any photo.
    fade = Image.new("L", (1, H))
    start = int(H * 0.45)
    for y in range(H):
        fade.putpixel((0, y), 0 if y < start else int(235 * ((y - start) / (H - start)) ** 1.1))
    shade = Image.new("RGB", (W, H), (10, 10, 12))
    img = Image.composite(shade, img, fade.resize((W, H)))
    draw = ImageDraw.Draw(img)
    f, lines, size = fit_headline(draw, clean_headline(headline), W - 2 * MARGIN)
    line_h = int(size * 1.12)
    name_f = font(MEDIUM, 30)
    y = H - MARGIN - 44 - len(lines) * line_h
    draw.rectangle((MARGIN, y - 34, MARGIN + 72, y - 26), fill=ACCENT)
    for ln in lines:
        draw.text((MARGIN, y), ln, font=f, fill=(250, 248, 244))
        y += line_h
    draw.text((MARGIN, H - MARGIN - 8), author, font=name_f, fill=(214, 210, 202), anchor="ls")
    return _png(img)


def type_cover(headline: str, author: str = AUTHOR) -> bytes:
    img = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(img)
    label_f = font(MEDIUM, 30)
    draw.text((MARGIN, MARGIN + 30), author.upper(), font=label_f, fill=MUTED, anchor="ls")
    f, lines, size = fit_headline(draw, clean_headline(headline), W - 2 * MARGIN, start=124, max_lines=5)
    line_h = int(size * 1.1)
    y = (H - len(lines) * line_h) // 2 - 20
    for ln in lines:
        draw.text((MARGIN, y), ln, font=f, fill=INK)
        y += line_h
    draw.rectangle((MARGIN, y + 30, MARGIN + 160, y + 44), fill=ACCENT)
    return _png(_grain(img, 0.025))
