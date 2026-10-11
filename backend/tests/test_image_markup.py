"""DCOL-FR13.4/13.7: numbered region marks drawn onto an image, locally."""

from __future__ import annotations

import io
import math
from itertools import combinations

from PIL import Image

from doceditor.image_markup import MAX_RENDER_SIDE, Mark, Region, place_tag, place_tags, render_marked_image, tag_geometry


def _png(width: int, height: int, color=(240, 240, 240)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


def _inside(cx: float, cy: float, r: float, width: int, height: int) -> bool:
    return r <= cx <= width - r and r <= cy <= height - r


def test_tag_sits_left_of_the_box_when_there_is_room():
    cx, cy, side = place_tag(Region(0.5, 0.5, 0.2, 0.2), 1000, 1000, radius=10, gap=3)

    assert side == "left"
    assert cx + 10 <= 500  # wholly left of the box's left edge
    assert 500 <= cy <= 520  # level with the top of the box


def test_tag_goes_above_when_the_left_is_clipped():
    cx, cy, side = place_tag(Region(0.005, 0.5, 0.2, 0.2), 1000, 1000, radius=10, gap=3)

    assert side == "above"
    assert cy + 10 <= 500  # wholly above the box's top edge
    assert _inside(cx, cy, 10, 1000, 1000)


def test_tag_goes_right_when_left_and_above_are_clipped():
    cx, cy, side = place_tag(Region(0.0, 0.0, 0.3, 0.2), 1000, 1000, radius=10, gap=3)

    assert side == "right"
    assert cx - 10 >= 300  # wholly right of the box's right edge
    assert _inside(cx, cy, 10, 1000, 1000)


def test_tag_moves_inside_the_corner_only_when_every_outside_spot_is_clipped():
    # A box hugging the left, top and right edges leaves no room outside it.
    cx, cy, side = place_tag(Region(0.0, 0.0, 1.0, 0.5), 1000, 1000, radius=10, gap=3)

    assert side == "inside"
    assert 0 + 10 <= cx <= 0 + 10 + 3 + 1
    assert 0 + 10 <= cy <= 0 + 10 + 3 + 1
    assert _inside(cx, cy, 10, 1000, 1000)


def test_a_point_keeps_its_tag_up_and_right_unless_clipped():
    _, _, side = place_tag(Region(0.5, 0.5), 1000, 1000, radius=10, gap=3)
    assert side == "up-right"

    cx, cy, side = place_tag(Region(0.999, 0.001), 1000, 1000, radius=10, gap=3)
    assert side != "up-right"
    assert _inside(cx, cy, 10, 1000, 1000)


def test_tags_on_close_neighbours_do_not_overlap():
    # Points on adjacent buttons of a screenshot, a box beside them; each tag keeps clear of the earlier ones.
    regions = [Region(0.50, 0.50), Region(0.51, 0.50), Region(0.50, 0.51), Region(0.47, 0.49, 0.02, 0.02)]
    tags = place_tags(regions, 1000, 1000, radius=10, gap=3)

    assert [side for _, _, side in tags][:2] == ["up-right", "down-right"]
    for (ax, ay, _), (bx, by, _) in combinations(tags, 2):
        assert math.hypot(ax - bx, ay - by) >= 2 * (10 + 2)


def test_a_crowded_tag_still_takes_a_spot_on_the_image():
    # With every outside spot taken, a tag overlaps rather than leaving the image.
    tags = place_tags([Region(0.0, 0.0, 1.0, 0.5)] * 2, 1000, 1000, radius=10, gap=3)

    assert [side for _, _, side in tags] == ["inside", "inside"]


def test_render_draws_the_tag_outside_the_box_and_leaves_the_corner_clear():
    data = _png(800, 600)
    png = render_marked_image(data, [Mark(1, Region(0.5, 0.5, 0.25, 0.25), "#ef4444")])
    img = Image.open(io.BytesIO(png)).convert("RGB")

    assert img.size == (800, 600)
    radius, gap = tag_geometry(800, 600)
    cx, cy, side = place_tag(Region(0.5, 0.5, 0.25, 0.25), 800, 600, radius=radius, gap=gap)
    assert side == "left"
    # The tag's fill is the author's colour; the content just inside the box's corner is untouched.
    r, g, b = img.getpixel((round(cx) - radius + 3, round(cy)))
    assert r > 200 and g < 120 and b < 120
    assert img.getpixel((400 + 12, 300 + 12)) == (240, 240, 240)


def test_render_caps_the_longest_side():
    png = render_marked_image(_png(4000, 1000), [Mark(1, Region(0.1, 0.1, 0.1, 0.1))])
    img = Image.open(io.BytesIO(png))

    assert max(img.size) == MAX_RENDER_SIDE
    assert img.format == "PNG"
