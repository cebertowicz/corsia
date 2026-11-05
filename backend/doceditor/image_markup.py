"""Draw numbered comment regions onto an image, locally (DCOL-FR13.7, FR13.8).

An agent that places a box gets the image back with its box drawn, so it can
check where the box landed; a delivered feedback batch carries the image with
every region comment's numbered box. Both read the same picture the viewer
shows: a box (or a point) in the author's colour with a numbered tag. Tags
that do not overlap stay on the picture; overlapping tags move outside the
nearest edge and join to their boxes. The viewer's ``imageGutter.ts`` uses the
same placement rule.

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

from core.image_resize import decode_slot, heif_supported
from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS
from infra.host_resources import image_decode_pixel_budget

# The longest side of the returned picture. A vision model downsamples anything
# larger before it reads it, so a bigger render only costs tokens and bytes.
# Keep equal to the 1600 in rasterBadgeTags in frontend/src/lib/imageGutter.ts. The picture is fitted again
# after its gutter is added; that scales tags with it, so they keep their place and only shrink, by under 4%.
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


# Keep equal to TAG_RADIUS and TAG_GAP in frontend/src/lib/imageRegions.ts.
TAG_RADIUS = 11
TAG_GAP = 3


def _scale_for(img: Image.Image) -> _Scale:
    return _Scale(
        stroke=_stroke(max(img.size)),
        radius=TAG_RADIUS,
        font=_font(max(11, round(TAG_RADIUS * 1.25))),
    )


TagSide = Literal["left", "above", "right", "inside", "up-right", "up-left", "down-right", "down-left"]


def _dot_radius(radius: float) -> float:
    """A point's dot radius, its white ring included, for tags of ``radius``."""
    return max(5, round(radius * 0.6)) + 2


# Another comment's mark in pixels: a point's dot (x, y, r), or a box (x0, y0, x1, y1).
TagMark = tuple[float, float, float] | tuple[float, float, float, float]


def place_tag(
    region: Region, width: int, height: int, *, radius: float, gap: float,
    avoid: Sequence[tuple[float, float]] = (), marks: Sequence[TagMark] = (),
) -> tuple[float, float, TagSide]:
    """Return the tag's centre and the side it took, for a ``width`` x ``height`` image.

    A spot is usable only when the whole tag, its white ring included, lies on
    the image. A box sits outside its top-left corner, then tries left of that
    corner, above it, then right of the box, and falls back to inside its corner.
    A point sits up-left of the point, then tries the other diagonals. A spot
    that would overlap a tag already centred at one of ``avoid``, or one of ``marks``, is passed over while another spot fits;
    tags weigh more than marks.
    """
    r = radius + 2  # the white ring drawn around the tag
    x0, y0 = region.x * width, region.y * height

    def fits(cx: float, cy: float) -> bool:
        """Whether a tag centred here stays whole inside the image."""
        return r <= cx <= width - r and r <= cy <= height - r

    if region.is_point:
        reach = radius * 1.4
        spots: list[tuple[float, float, TagSide]] = [
            (x0 - reach, y0 - reach, "up-left"),
            (x0 + reach, y0 - reach, "up-right"),
            (x0 + reach, y0 + reach, "down-right"),
            (x0 - reach, y0 + reach, "down-left"),
        ]
    else:
        x1 = x0 + region.w * width
        spots = [
            (x0 - gap - r, y0 - gap - r, "up-left"),
            (x0 - gap - r, y0 + r, "left"),
            (x0 + r, y0 - gap - r, "above"),
            (x1 + gap + r, y0 + r, "right"),
        ]
    def clear(cx: float, cy: float) -> bool:
        return all(math.hypot(cx - ox, cy - oy) >= 2 * r for ox, oy in avoid)

    def open_(cx: float, cy: float) -> bool:
        for mark in marks:
            if len(mark) == 3:
                mx, my, mr = mark
                if math.hypot(cx - mx, cy - my) < r + mr:
                    return False
            else:
                mx0, my0, mx1, my1 = mark
                if math.hypot(cx - min(max(cx, mx0), mx1), cy - min(max(cy, my0), my1)) < r:
                    return False
        return True

    for cx, cy, side in spots:
        if fits(cx, cy) and clear(cx, cy) and open_(cx, cy):
            return cx, cy, side
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
    dot: float | None = None,
) -> list[tuple[float, float, TagSide]]:
    """Every tag on one image, placed in ``regions``' order.

    Each keeps clear of the tags before it and of every other comment's mark;
    ``dot`` is a point's dot radius with its ring, the renderer's by default.
    """
    dot = _dot_radius(radius) if dot is None else dot
    marks: list[TagMark] = [
        (r.x * width, r.y * height, dot) if r.is_point
        else (r.x * width, r.y * height, (r.x + r.w) * width, (r.y + r.h) * height)
        for r in regions
    ]
    placed: list[tuple[float, float, TagSide]] = []
    for index, region in enumerate(regions):
        placed.append(place_tag(region, width, height, radius=radius, gap=gap,
                                avoid=[(x, y) for x, y, _ in placed],
                                marks=[m for other, m in enumerate(marks) if other != index]))
    return placed


def gutter_tags(
    regions: Sequence[Region], width: int, height: int, *, radius: float, gap: float,
) -> list[tuple[float, float, str, float, float]]:
    """Place tags outside the closest edge, in anchor order like the viewer."""
    r = radius + 2
    spacing = 2 * r + gap
    tags: list[list] = []
    for region in regions:
        x0, y0 = region.x * width, region.y * height
        x1, y1 = (region.x + region.w) * width, (region.y + region.h) * height
        choices = [("left", x0), ("right", width - x1), ("above", y0), ("below", height - y1)]
        side = min(choices, key=lambda item: item[1])[0]
        if side == "left":
            tags.append([-gap - r, y0, side, x0, y0])
        elif side == "right":
            tags.append([width + gap + r, y0, side, x1, y0])
        elif side == "above":
            tags.append([x0, -gap - r, side, x0, y0])
        else:
            tags.append([x0, height + gap + r, side, x0, y1])
    for side in ("left", "right", "above", "below"):
        vertical = side in ("left", "right")
        extent, axis = (height, 1) if vertical else (width, 0)
        lower, upper = min(r, extent / 2), max(extent - r, extent / 2)
        entries = sorted((tag for tag in tags if tag[2] == side), key=lambda tag: tag[axis])
        positions: list[float] = []
        for tag in entries:
            desired = max(lower, min(upper, tag[axis]))
            positions.append(max(desired, positions[-1] + spacing if positions else lower))
        for i in range(len(positions) - 1, -1, -1):
            positions[i] = min(positions[i], upper if i == len(positions) - 1 else positions[i + 1] - spacing)
        if positions and positions[0] < lower:
            positions = [lower + (upper - lower) * i / max(1, len(positions) - 1) for i in range(len(positions))]
        for tag, along in zip(entries, positions, strict=True):
            tag[axis] = along
    return [tuple(tag) for tag in tags]


def badge_tags(
    regions: Sequence[Region], width: int, height: int, *, radius: float, gap: float,
) -> list[tuple[float, float, str, float | None, float | None]]:
    """Keep ordinary positions on the picture; move overlapping badges outside."""
    positions = [place_tags([region], width, height, radius=radius, gap=gap)[0] for region in regions]
    inside: list[tuple[float, float]] = []
    outside: list[int] = []
    for index, (x, y, _) in enumerate(positions):
        if any(math.hypot(x - ox, y - oy) < 2 * (radius + 2) for ox, oy in inside):
            outside.append(index)
        else:
            inside.append((x, y))
    result: list[tuple[float, float, str, float | None, float | None]] = [
        (x, y, side, None, None) for x, y, side in positions
    ]
    edge = gutter_tags([regions[i] for i in outside], width, height, radius=radius, gap=gap)
    for index, tag in zip(outside, edge, strict=True):
        result[index] = tag
    return result


def _draw_region(draw: ImageDraw.ImageDraw, mark: Mark, img: Image.Image, scale: _Scale) -> None:
    color = _rgb(mark.color)
    region = mark.region
    x0, y0 = region.x * img.width, region.y * img.height
    if region.is_point:
        dot = _dot_radius(scale.radius) - 2
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
    draw.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2), outline="white", width=2)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*color, 204))
    # Match the viewer's --image-region-ink token.
    draw.text((cx, cy), str(mark.number), fill=(17, 17, 17), font=scale.font, anchor="mm")


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
        # The budget is one slot's share of memory, so the full frame is held only inside a slot.
        with decode_slot():
            source.load()
            # Upright, as the viewer's <img> shows it, so fractions land on the same pixels.
            img = _fit(ImageOps.exif_transpose(source).convert("RGB"))
    draw = ImageDraw.Draw(img)
    scale = _scale_for(img)
    for mark in marks:
        _draw_region(draw, mark, img, scale)
    tags = badge_tags([mark.region for mark in marks], img.width, img.height, radius=scale.radius, gap=TAG_GAP)
    gutter = math.ceil(2 * (scale.radius + 2) + TAG_GAP) if any(tag[3] is not None for tag in tags) else 0
    expanded = Image.new("RGB", (img.width + 2 * gutter, img.height + 2 * gutter), "white")
    expanded.paste(img, (gutter, gutter))
    draw = ImageDraw.Draw(expanded, "RGBA")
    for mark, (cx, cy, _side, ax, ay) in zip(marks, tags, strict=True):
        if ax is not None and ay is not None:
            draw.line((cx + gutter, cy + gutter, ax + gutter, ay + gutter), fill=_rgb(mark.color), width=1)
        _draw_tag(draw, mark, (cx + gutter, cy + gutter), scale)
    expanded = _fit(expanded)
    out = io.BytesIO()
    expanded.save(out, format="PNG", optimize=True)
    return out.getvalue()
