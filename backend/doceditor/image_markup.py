"""Draw numbered comment regions onto an image, locally (DCOL-FR13.7, FR13.8).

An agent that places a box gets the image back with its box drawn, so it can
check where the box landed; a delivered feedback batch carries the image with
every region comment's numbered box. Both read the same picture the viewer
shows: a box (or a point) in the author's colour with its number in a tag that
never covers what the box points at. The tag tries, in order, left of the box's
top-left corner, above it, then right of the box; only when the image edge clips
all three does it sit inside the box's corner, over the box's padding. The
viewer's ``imageRegions.ts`` places tags by the same rule.

Rendering is local Pillow work on bytes the caller already read; nothing is
fetched. Callers run it off the event loop.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

from core.image_resize import heif_supported
from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS
from infra.host_resources import image_decode_pixel_budget

# The longest side of the returned picture. A vision model downsamples anything
# larger before it reads it, so a bigger render only costs tokens and bytes.
MAX_RENDER_SIDE = 1600

_DEFAULT_COLOR = "#7c3aed"


@dataclass(frozen=True)
class Region:
    """A box or a point as fractions of the image, origin top-left.

    A point has zero width and height.
    """

    x: float
    y: float
    w: float = 0.0
    h: float = 0.0

    @property
    def is_point(self) -> bool:
        """A point has no width or height."""
        return self.w <= 0 and self.h <= 0


@dataclass(frozen=True)
class Mark:
    number: int
    region: Region
    color: str = _DEFAULT_COLOR


def _rgb(color: str) -> tuple[int, int, int]:
    """Any CSS colour Pillow reads (hex, rgb(), hsl()), else the default."""
    from PIL import ImageColor

    try:
        return ImageColor.getrgb(color)[:3]
    except ValueError:
        return ImageColor.getrgb(_DEFAULT_COLOR)[:3]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _fit(img: Image.Image) -> Image.Image:
    longest = max(img.size)
    if longest <= MAX_RENDER_SIDE:
        return img
    scale = MAX_RENDER_SIDE / longest
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS)


@dataclass(frozen=True)
class _Scale:
    stroke: int
    radius: int
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont


def _stroke(longest: int) -> int:
    return max(2, round(longest * 0.0022))


def tag_geometry(width: int, height: int) -> tuple[int, int]:
    """The tag radius and its gap from the box, in pixels of the rendered image."""
    longest = max(width, height)
    return max(10, round(longest * 0.011)), _stroke(longest) + 1


def _scale_for(img: Image.Image) -> _Scale:
    radius, _gap = tag_geometry(*img.size)
    return _Scale(
        stroke=_stroke(max(img.size)),
        radius=radius,
        font=_font(max(11, round(radius * 1.25))),
    )


TagSide = Literal["left", "above", "right", "inside", "up-right", "up-left", "down-right", "down-left"]


def place_tag(
    region: Region, width: int, height: int, *, radius: float, gap: float,
    avoid: Sequence[tuple[float, float]] = (),
) -> tuple[float, float, TagSide]:
    """Return the tag's centre and the side it took, for a ``width`` x ``height`` image.

    A spot is usable only when the whole tag, its white ring included, lies on
    the image. A box tries left of its top-left corner, above it, then right of
    the box, and falls back to inside its corner. A point tries up-right, then
    the other diagonals. A spot that would overlap a tag already centred at one
    of ``avoid`` is passed over while another spot fits.
    """
    r = radius + 2  # the white ring drawn around the tag
    x0, y0 = region.x * width, region.y * height

    def fits(cx: float, cy: float) -> bool:
        """Whether a tag centred here stays whole inside the image."""
        return r <= cx <= width - r and r <= cy <= height - r

    if region.is_point:
        reach = radius * 1.4
        spots: list[tuple[float, float, TagSide]] = [
            (x0 + reach, y0 - reach, "up-right"),
            (x0 - reach, y0 - reach, "up-left"),
            (x0 + reach, y0 + reach, "down-right"),
            (x0 - reach, y0 + reach, "down-left"),
        ]
    else:
        x1 = x0 + region.w * width
        spots = [
            (x0 - gap - r, y0 + r, "left"),
            (x0 + r, y0 - gap - r, "above"),
            (x1 + gap + r, y0 + r, "right"),
        ]
    def clear(cx: float, cy: float) -> bool:
        return all(math.hypot(cx - ox, cy - oy) >= 2 * r for ox, oy in avoid)

    for cx, cy, side in spots:
        if fits(cx, cy) and clear(cx, cy):
            return cx, cy, side
    for cx, cy, side in spots:
        if fits(cx, cy):
            return cx, cy, side
    if region.is_point:
        cx, cy, side = spots[0]
    else:
        cx, cy, side = x0 + gap + radius, y0 + gap + radius, "inside"
    # A tag never leaves the image, even on one smaller than the tag itself.
    cx = min(max(cx, min(r, width / 2)), max(width - r, width / 2))
    cy = min(max(cy, min(r, height / 2)), max(height - r, height / 2))
    return cx, cy, side


def place_tags(
    regions: Sequence[Region], width: int, height: int, *, radius: float, gap: float,
) -> list[tuple[float, float, TagSide]]:
    """Every tag on one image, placed in ``regions``' order, each clear of those before it."""
    placed: list[tuple[float, float, TagSide]] = []
    for region in regions:
        placed.append(place_tag(region, width, height, radius=radius, gap=gap,
                                avoid=[(x, y) for x, y, _ in placed]))
    return placed


def _draw_region(draw: ImageDraw.ImageDraw, mark: Mark, img: Image.Image, scale: _Scale) -> None:
    color = _rgb(mark.color)
    region = mark.region
    x0, y0 = region.x * img.width, region.y * img.height
    if region.is_point:
        dot = max(5, round(scale.radius * 0.6))
        draw.ellipse((x0 - dot - 2, y0 - dot - 2, x0 + dot + 2, y0 + dot + 2), fill="white")
        draw.ellipse((x0 - dot, y0 - dot, x0 + dot, y0 + dot), fill=color)
        return
    x1, y1 = x0 + region.w * img.width, y0 + region.h * img.height
    s = scale.stroke
    draw.rectangle((x0 - s, y0 - s, x1 + s, y1 + s), outline="white", width=1)
    draw.rectangle((x0, y0, x1, y1), outline=color, width=s)


def _draw_tag(draw: ImageDraw.ImageDraw, mark: Mark, centre: tuple[float, float], scale: _Scale) -> None:
    color = _rgb(mark.color)
    r = scale.radius
    cx, cy = centre
    draw.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2), fill="white")
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    draw.text((cx, cy), str(mark.number), fill="white", font=scale.font, anchor="mm")


def _check_budget(source: Image.Image) -> None:
    budget = image_decode_pixel_budget(MAX_CONCURRENT_IMAGE_NORMALIZATIONS)
    if source.width * source.height > budget:
        raise ValueError(f"image is too large to mark up ({source.width}x{source.height})")


def check_markable(path: Path) -> None:
    """Raise what ``render_marked_image`` would for the file at ``path``, from its header alone.

    Opening reads only the header, so a caller holding a lock can learn whether
    a copy can be drawn without decoding the image.
    """
    heif_supported()
    with Image.open(path) as source:
        _check_budget(source)


def render_marked_image(data: bytes, marks: Sequence[Mark]) -> bytes:
    """Return a PNG of ``data`` with each mark's region and numbered tag drawn.

    Boxes are drawn first and tags last, so no box covers another's number.
    """
    heif_supported()
    with Image.open(io.BytesIO(data)) as source:
        _check_budget(source)
        source.draft("RGB", (MAX_RENDER_SIDE, MAX_RENDER_SIDE))
        source.load()
        # Upright, as the viewer's <img> shows it, so fractions land on the same pixels.
        img = _fit(ImageOps.exif_transpose(source).convert("RGB"))
    draw = ImageDraw.Draw(img)
    scale = _scale_for(img)
    for mark in marks:
        _draw_region(draw, mark, img, scale)
    tags = place_tags([mark.region for mark in marks], img.width, img.height,
                      radius=scale.radius, gap=scale.stroke + 1)
    for mark, (cx, cy, _side) in zip(marks, tags, strict=True):
        _draw_tag(draw, mark, (cx, cy), scale)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
