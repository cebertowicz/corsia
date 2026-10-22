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


class _ScalarTool(_PayloadTool):
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
                "listed": {"type": ["integer", "null"]},
            },
        }


def test_scalar_text_decodes_only_when_it_spells_the_declared_type():
    tool = _ScalarTool()

    assert tool.decode_string_arguments({"count": "52", "ratio": "0.5", "flag": "false"}) == {
        "count": 52, "ratio": 0.5, "flag": False,
    }
    kept = [
        {"count": "5.0"}, {"count": "true"}, {"count": " 52"}, {"count": "52x"},
        {"ratio": "NaN"}, {"ratio": "true"},
        {"flag": "1"}, {"flag": "yes"}, {"flag": "True"},
        {"listed": "52"},
    ]
    for params in kept:
        assert tool.decode_string_arguments(params) == params
    assert tool.validate_params({"flag": "yes"}) == ["flag should be boolean"]


def test_deferred_automation_schedule_called_by_name_accepts_text_max_runs():
    """A local model calling the deferred tool directly sends max_runs '52'; it was refused 7 times."""
    from tools.automations import AutomationScheduleTool

    tool = AutomationScheduleTool()
    decoded = tool.decode_string_arguments({"kind": "cadence", "spec": "86400", "max_runs": "52"})

    assert decoded["max_runs"] == 52 and decoded["spec"] == "86400"
    assert tool.validate_params(decoded) == []


@pytest.mark.asyncio
async def test_registry_passes_a_direct_call_with_text_max_runs_to_the_tool(monkeypatch):
    from tools.automations import AutomationScheduleTool

    registry = ToolRegistry()
    registry.register(AutomationScheduleTool())

    async def no_identity():
        return None, "Error: identity sentinel."

    monkeypatch.setattr(registry, "_load_thread_context", no_identity)

    result = await registry.execute(
        "automation_schedule", {"kind": "cadence", "spec": "86400", "max_runs": "52"},
    )

    assert result.startswith("Error: identity sentinel.")


@pytest.mark.asyncio
async def test_registry_decodes_before_it_validates(monkeypatch):
    registry = ToolRegistry()
    registry.register(_PayloadTool())

    async def no_identity():
        return None, "Error: identity sentinel."

    monkeypatch.setattr(registry, "_load_thread_context", no_identity)

    result = await registry.execute("payload", {"payload": '{"a": 1}'})

    assert result.startswith("Error: identity sentinel.")
