import io
import json
import math
from pathlib import Path

import pytest

from PIL import Image

from doceditor.image_markup import Mark, Region, badge_tags, render_marked_image


def test_badge_positions_match_viewer_contract():
    fixture = Path(__file__).resolve().parents[2] / 'frontend/src/lib/__tests__/fixtures/image-badge-parity.json'
    for case in json.loads(fixture.read_text()):
        regions = [Region(**region) for region in case['regions']]
        tags = badge_tags(regions, case['width'], case['height'], radius=case['radius'], gap=case['gap'])
        actual = [dict(x=x, y=y, side=side, **({'anchor': {'x': ax, 'y': ay}} if ax is not None else {})) for x, y, side, ax, ay in tags]
        assert actual == case['expected']


@pytest.mark.parametrize('case', ['pair', 'hundred'])
def test_actual_renderer_matches_viewer_natural_sides_without_overlapping_discs(case, monkeypatch):
    from doceditor import image_markup

    fixture = Path(__file__).resolve().parents[2] / 'frontend/src/lib/__tests__/fixtures/image-natural-parity.json'
    expected = json.loads(fixture.read_text())[case]
    regions = ([Region(.4, .4, .02, .02), Region(.42, .4, .02, .02)] if case == 'pair' else
               [Region(.15 + (i % 10) * .07, .2 + (i % 3) * .2, .02, .1) for i in range(1, 101)])
    actual = []
    draw_radii = []
    real_tags, real_draw = image_markup.badge_tags, image_markup._draw_tag

    def capture_tags(*args, **kwargs):
        assert kwargs == {'radius': 11, 'gap': 3}
        actual.extend(real_tags(*args, **kwargs))
        return actual

    def capture_draw(draw, mark, centre, scale):
        draw_radii.append(scale.radius)
        return real_draw(draw, mark, centre, scale)

    monkeypatch.setattr(image_markup, 'badge_tags', capture_tags)
    monkeypatch.setattr(image_markup, '_draw_tag', capture_draw)
    source = io.BytesIO()
    Image.new('RGB', (1600, 1000), 'white').save(source, format='PNG')
    render_marked_image(source.getvalue(), [Mark(i, region) for i, region in enumerate(regions, 1)])
    assert [tag[2] for tag in actual] == expected
    assert draw_radii == [11] * len(regions)
    assert all(math.dist(a[:2], b[:2]) >= 22 for i, a in enumerate(actual) for b in actual[i + 1:])


def test_capped_4000_pixel_image_matches_viewer_at_point_four_zoom(monkeypatch):
    from doceditor import image_markup

    regions = [Region(.4, .4, .002, .02), Region(.4065, .4, .002, .02)]
    actual = []
    real_tags = image_markup.badge_tags

    def capture_tags(*args, **kwargs):
        assert args[1:3] == (1600, 400)
        assert kwargs == {'radius': 11, 'gap': 3}
        actual.extend(real_tags(*args, **kwargs))
        return actual

    monkeypatch.setattr(image_markup, 'badge_tags', capture_tags)
    source = io.BytesIO()
    Image.new('RGB', (4000, 1000), 'white').save(source, format='PNG')
    render_marked_image(source.getvalue(), [Mark(i, region) for i, region in enumerate(regions, 1)])
    assert [tag[2] for tag in actual] == ['up-left', 'above']
    assert math.dist(actual[0][:2], actual[1][:2]) >= 22


def test_single_badge_stays_on_picture_and_is_translucent():
    source = io.BytesIO()
    Image.new('RGB', (800, 500), 'white').save(source, format='PNG')
    result = Image.open(io.BytesIO(render_marked_image(source.getvalue(), [Mark(1, Region(0, 0, 1, 0.5), '#000000')])))
    assert result.size == (800, 500)
    # Disc fill against white must retain some of the image rather than become black.
    assert 0 < result.getpixel((15, 20))[0] < 255


def test_agent_copy_uses_the_viewer_numeral_ink():
    source = io.BytesIO()
    Image.new('RGB', (800, 500), 'white').save(source, format='PNG')
    result = Image.open(io.BytesIO(render_marked_image(source.getvalue(), [Mark(1, Region(0, 0, 1, 0.5), '#06b6d4')])))
    pixels = (result.getpixel((x, y)) for x in range(4, 25) for y in range(4, 25))
    assert any(max(pixel) <= 25 for pixel in pixels)  # antialiasing blends the small numeral


def test_badge_blends_with_the_underlying_picture():
    source = io.BytesIO()
    Image.new('RGB', (800, 500), (255, 0, 0)).save(source, format='PNG')
    # A colour whose digit already reads keeps its fill: 80% of it over the red picture.
    result = Image.open(io.BytesIO(render_marked_image(source.getvalue(), [Mark(1, Region(0, 0, 1, 0.5), '#22c55e')])))
    assert result.getpixel((15, 20)) == pytest.approx((78, 158, 75), abs=1)
