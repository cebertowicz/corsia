"""The marked-up copy's colours and tag spots match the viewer's.

``doceditor/image_regions.py`` and ``doceditor/image_markup.py`` port three viewer
rules (``docPresence.ts`` ``agentColor``, ``projectColors.ts`` ``projectColor``,
``imageRegions.ts`` ``placeTag``) so the image an agent gets back looks like the
one the reader sees. This test and the frontend's
``image-region-parity.test.ts`` both hold their side to one committed fixture, so
a change to either side fails until the other follows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doceditor.image_markup import Region, place_tag
from doceditor.image_regions import _project_color, agent_color

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend/src/lib/__tests__/fixtures/image-region-parity.contract.json"
)
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CONTRACT["agent_colors"], ids=lambda c: c["thread_id"] or "empty")
def test_agent_color_matches_viewer(case):
    assert agent_color(case["thread_id"]) == case["color"]


@pytest.mark.parametrize("case", CONTRACT["project_colors"], ids=lambda c: str(c["project_id"]))
def test_project_color_matches_viewer(case):
    assert _project_color(case["project_id"], None) == case["color"]


@pytest.mark.parametrize("case", CONTRACT["tag_placements"], ids=lambda c: c["case"])
def test_tag_placement_matches_viewer(case):
    region = case["region"]
    x, y, side = place_tag(
        Region(region["x"], region["y"], region["w"], region["h"]),
        case["width"], case["height"], radius=case["radius"], gap=case["gap"],
    )
    expected = case["expected"]
    assert side == expected["side"]
    assert x == pytest.approx(expected["x"], abs=1e-3)
    assert y == pytest.approx(expected["y"], abs=1e-3)
