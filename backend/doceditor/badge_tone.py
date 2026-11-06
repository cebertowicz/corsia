"""The numbered disc's fill in a marked-up copy: the viewer's ``badgeFill`` (frontend/src/lib/imageRegions.ts), stepped the same way."""

from __future__ import annotations

import math
from typing import Sequence

# The digit's ink (the viewer's --image-region-ink), the disc's fill opacity and the two backdrops it is read over.
# Keep equal to BADGE_INK, BADGE_ALPHA and BADGE_BACKDROPS in frontend/src/lib/imageRegions.ts.
INK = (17, 17, 17)
ALPHA = 0.8
BACKDROPS = ((0, 0, 0), (255, 255, 255))
MIN_CONTRAST = 4.5


def luminance(rgb: Sequence[float]) -> float:
    r, g, b = (c / 255 / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: Sequence[float], b: Sequence[float]) -> float:
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _to_hsl(rgb: Sequence[float]) -> tuple[float, float, float]:
    r, g, b = (c / 255 for c in rgb)
    high, low = max(r, g, b), min(r, g, b)
    light = (high + low) / 2
    if high == low:
        return 0.0, 0.0, light
    d = high - low
    sat = d / (2 - high - low) if light > 0.5 else d / (high + low)
    hue = (g - b) / d + (6 if g < b else 0) if high == r else (b - r) / d + 2 if high == g else (r - g) / d + 4
    return hue * 60, sat, light


def _from_hsl(h: float, s: float, light: float) -> tuple[int, int, int]:
    chroma = (1 - abs(2 * light - 1)) * s
    x = chroma * (1 - abs(((h / 60) % 2) - 1))
    m = light - chroma / 2
    r, g, b = ((chroma, x, 0) if h < 60 else (x, chroma, 0) if h < 120 else (0, chroma, x) if h < 180
               else (0, x, chroma) if h < 240 else (x, 0, chroma) if h < 300 else (chroma, 0, x))
    return math.floor((r + m) * 255 + 0.5), math.floor((g + m) * 255 + 0.5), math.floor((b + m) * 255 + 0.5)


def badge_fill(own: tuple[int, int, int]) -> tuple[int, int, int]:
    """The author's colour, stepped lighter only as far as the digit needs to meet 4.5:1 against the fill at 80% over
    black and over white; a colour that already does is returned as it came."""

    def reads(fill: Sequence[float]) -> bool:
        return all(
            contrast(INK, [c * ALPHA + bg * (1 - ALPHA) for c, bg in zip(fill, backdrop)]) >= MIN_CONTRAST
            for backdrop in BACKDROPS
        )

    if reads(own):
        return own
    hue, sat, light = _to_hsl(own)
    step = light + 0.005
    while step <= 1:
        tone = _from_hsl(hue, sat, step)
        if reads(tone):
            return tone
        step += 0.005
    return 255, 255, 255
