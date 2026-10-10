"""A multi-type union argument validates against the member the value matches.

`recommendation` is `["string", "array"]` so one field carries a pick-one id or
a pick-any list. The validator used to continue with the first non-null member,
so a list was refused as "should be string" and its items were never checked.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools.base import Tool


class _UnionTool(Tool):
    @property
    def name(self) -> str:
        return "union"

    @property
    def description(self) -> str:
        return "Take a string or a list of ids, and an optional count."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pick": {
                    "type": ["string", "array"],
                    "items": {"type": "string", "enum": ["A", "B"]},
                },
                "count": {"type": ["integer", "null"], "minimum": 1},
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


@pytest.mark.parametrize("value", ["A", "anything", [], ["A"], ["A", "B"]])
def test_a_value_matching_one_member_of_the_union_is_accepted(value):
    assert _UnionTool().validate_params({"pick": value}) == []


def test_a_list_with_a_bad_item_is_refused_at_that_item():
    errors = _UnionTool().validate_params({"pick": ["A", "E"]})

    assert errors == ["pick[1] must be one of ['A', 'B']"]


@pytest.mark.parametrize("value", [3, {"id": "A"}, True])
def test_a_value_matching_no_member_is_refused(value):
    errors = _UnionTool().validate_params({"pick": value})

    assert errors == ["pick should be one of ['string', 'array']"]


def test_a_null_union_behaves_as_before():
    tool = _UnionTool()

    assert tool.validate_params({"count": None}) == []
    assert tool.validate_params({"count": 2}) == []
    assert tool.validate_params({"count": 0}) == ["count must be >= 1"]
    assert tool.validate_params({"count": "2"}) == ["count should be one of ['integer']"]


def test_the_unions_already_shipping_validate_exactly_as_before():
    from tools.artifact_list import ArtifactListTool
    from tools.artifacts import ArtifactResolveTool

    listing, resolve = ArtifactListTool(), ArtifactResolveTool()

    assert listing.validate_params({"owned": True, "state": None, "cursor": None}) == []
    assert listing.validate_params({"owned": True, "state": "overdue", "cursor": "c"}) == []
    assert listing.validate_params({"owned": True, "state": "stale"}) == [
        "state must be one of ['overdue', 'never_verified', None]",
    ]
    assert listing.validate_params({"owned": True, "cursor": 3}) == [
        "cursor should be one of ['string']",
    ]
    assert resolve.validate_params({"artifact_id": None, "path": "a.md"}) == []
    assert resolve.validate_params({"artifact_id": 7}) == [
        "artifact_id should be one of ['string']",
    ]
