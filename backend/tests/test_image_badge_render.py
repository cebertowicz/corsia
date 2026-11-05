import io
import json
from pathlib import Path

from PIL import Image

from doceditor.image_markup import Mark, Region, badge_tags, render_marked_image


def test_badge_positions_match_viewer_contract():
    fixture = Path(__file__).resolve().parents[2] / 'frontend/src/lib/__tests__/fixtures/image-badge-parity.json'
    for case in json.loads(fixture.read_text()):
        regions = [Region(**region) for region in case['regions']]
        tags = badge_tags(regions, case['width'], case['height'], radius=case['radius'], gap=case['gap'])
        actual = [dict(x=x, y=y, side=side, **({'anchor': {'x': ax, 'y': ay}} if ax is not None else {})) for x, y, side, ax, ay in tags]
        assert actual == case['expected']


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
    result = Image.open(io.BytesIO(render_marked_image(source.getvalue(), [Mark(1, Region(0, 0, 1, 0.5), '#000000')])))
    red, green, blue = result.getpixel((15, 20))
    assert 0 < red < 255
    assert green == blue == 0
