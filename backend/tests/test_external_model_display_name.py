"""The external model display-name rule, shared with frontend/src/lib/modelDisplayName.ts."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from subagents.model_display_name import external_model_display_name  # noqa: E402

_CASES = json.loads(
    (Path(__file__).parents[2] / "testdata" / "model-display-names.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_display_name_matches_the_shared_fixture(case):
    assert external_model_display_name(case["value"]) == case["expected"]


def test_empty_model_has_no_name():
    assert external_model_display_name(None) is None
    assert external_model_display_name("") is None
