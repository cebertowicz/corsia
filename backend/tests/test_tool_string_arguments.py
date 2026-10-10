"""An object argument a model sends as JSON text is the argument (#2453).

Qwen on the native harness serializes a nested argument as a string, so every
plan_decision draft was refused with 'params should be object' twice.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tools.base import Tool
from tools.plans import PlanDecisionTool, PlanUpdateTool
from tools.registry import ToolRegistry


class _PayloadTool(Tool):
    @property
    def name(self) -> str:
        return "payload"

    @property
    def description(self) -> str:
        return "Take one object, one array and one string."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "payload": {"type": "object"},
                "items": {"type": "array"},
                "text": {"type": "string"},
                "shaped": {"oneOf": [{"type": "object"}, {"type": "object"}]},
                "either": {"oneOf": [{"type": "object"}, {"type": "string"}]},
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_object_and_array_text_decode_to_their_declared_types():
    decoded = _PayloadTool().decode_string_arguments({
        "payload": '{"a": 1}', "items": "[1, 2]", "text": '{"kept": true}',
        "shaped": '{"b": 2}', "either": '{"kept": true}',
    })

    # A oneOf whose every branch is an object is an object; a mixed one is not.
    assert decoded == {
        "payload": {"a": 1}, "items": [1, 2], "text": '{"kept": true}',
        "shaped": {"b": 2}, "either": '{"kept": true}',
    }


class _ExternalSchemaTool(_PayloadTool):
    """Schema shapes an external (MCP) tool may send, which name no single container type."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "listed": {"oneOf": [{"type": ["object", "null"]}, {"type": "object"}]},
                "boolean": {"oneOf": [True, {"type": "object"}]},
                "empty": {"oneOf": []},
            },
        }


def test_external_schema_shapes_are_left_as_they_arrive():
    """A list-typed or boolean oneOf branch neither raises nor decodes."""
    tool = _ExternalSchemaTool()
    params = {"listed": '{"a": 1}', "boolean": '{"a": 1}', "empty": '{"a": 1}'}

    assert tool.decode_string_arguments(params) == params
    assert tool.validate_params(params) == []


def test_text_that_is_not_the_declared_type_is_left_for_validation():
    tool = _PayloadTool()
    params = {"payload": "[1]", "items": "not json"}

    assert tool.decode_string_arguments(params) == params
    assert tool.validate_params(params) == [
        "payload should be object, not text: pass the object itself",
        "items should be array, not text: pass the array itself",
    ]


@pytest.mark.parametrize("tool_type, operation", [
    (PlanDecisionTool, "draft"),
    (PlanUpdateTool, "start"),
])
def test_plan_tool_string_params_are_its_params(tool_type, operation):
    tool = tool_type()
    params = {"operation": operation, "plan_id": "p", "mutation_id": "m"}
    as_text = {**params, "params": json.dumps({"node_id": "n"})}

    decoded = tool.decode_string_arguments(as_text)

    assert decoded["params"] == {"node_id": "n"}
    assert "params should be object" not in tool.validate_params(decoded)


def test_a_refusal_names_each_error_once():
    tool = PlanDecisionTool()
    errors = tool.validate_params({
        "operation": "draft", "plan_id": "p", "mutation_id": "m", "params": 7,
    })

    assert errors == ["params should be object"]


@pytest.mark.asyncio
async def test_registry_decodes_before_it_validates(monkeypatch):
    registry = ToolRegistry()
    registry.register(_PayloadTool())

    async def no_identity():
        return None, "Error: identity sentinel."

    monkeypatch.setattr(registry, "_load_thread_context", no_identity)

    result = await registry.execute("payload", {"payload": '{"a": 1}'})

    assert result.startswith("Error: identity sentinel.")
