"""Workflow schemas can declare the result limits used by the screen."""

import json

import pytest

from core.workflow_result import ResultInvalid, check_result, schema_errors


SCHEMA = {"type": "array", "maxItems": 5,
          "items": {"type": "string", "maxLength": 200}}


def test_declared_limits_are_checked():
    assert schema_errors(SCHEMA) == []
    assert check_result(json.dumps(["x" * 200] * 5), SCHEMA) == ["x" * 200] * 5
    with pytest.raises(ResultInvalid):
        check_result(json.dumps(["x"] * 6), SCHEMA)
    with pytest.raises(ResultInvalid):
        check_result(json.dumps(["x" * 201]), SCHEMA)


def test_extra_properties_rejected_only_when_declared():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert check_result('{"x":"ok","extra":"kept"}', schema)["extra"] == "kept"
    schema["additionalProperties"] = False
    assert schema_errors(schema) == []
    with pytest.raises(ResultInvalid):
        check_result('{"x":"ok","extra":"bad"}', schema)
    assert check_result('{"x":"ok"}', schema) == {"x": "ok"}
    assert schema_errors({"type": "object", "additionalProperties": "no"})


@pytest.mark.parametrize("limit", [-1, True, "5"])
def test_invalid_limit_declarations_are_rejected(limit):
    assert schema_errors({"type": "array", "maxItems": limit, "items": {"type": "string"}})
    assert schema_errors({"type": "string", "maxLength": limit})
