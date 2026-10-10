"""The same `next` bytes reach native, Claude Code and Codex, and survive previews.

`next` is part of the tool's own JSON result, so parity comes from construction:
the native registry string, the Claude Code bridge's persisted delivery and the
Codex dynamic-tool response all carry the identical block. A resumed Codex
thread may keep its thread-start schema without `next_node_id`, so a close
without it must still be actionable from the block's field names alone. A large
close result is previewed head-and-tail, and the block stays whole in the head.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from core.tool_result_invocation import with_invocation_reference
from infra.database import get_db
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_close_next_helpers import (
    assert_block_shape,
    close_params,
    queue_active,
    serialized,
)
from tests.plan_execution_helpers import add_task, open_turn
from tools.plans import PlanUpdateTool
from tools.registry import ToolRegistry


class CapturingRegistry(ToolRegistry):
    """A registry that keeps each string it returns, to compare delivered bytes."""

    def __init__(self) -> None:
        super().__init__()
        self.returned: list[str] = []

    async def execute(self, name, params):
        """Execute through the real registry and remember the exact result."""
        result = await super().execute(name, params)
        self.returned.append(result)
        return result


def _scene(thread_id: str, *, criteria: str | None = None) -> dict:
    """Select Task A in a live Turn beside queued, pending and blocked work."""
    db = get_db()
    try:
        identity = open_turn(db, thread_id, f"turn-{thread_id}")
        plan = service.ensure_plan("thread", thread_id, db=db)
        task_a = service.add_node(
            plan.id, kind="task", title="Task A",
            acceptance_criteria=criteria or "Task A is verified", db=db,
        )
        queued = add_task(db, plan.id, "Queued work")
        task_b = add_task(db, plan.id, "Task B")
        blocked = add_task(db, plan.id, "Blocked work")
        service.add_dependency(blocked.id, task_b.id, db=db)
        admission.activate(
            task_a.id, expected_revision=task_a.revision, actor_id=thread_id,
            provenance="test", db=db,
        )
        db.commit()
        queue_active(db, queued.id)
        ids = {task_a.id: "<A>", queued.id: "<Q>", task_b.id: "<B>", blocked.id: "<D>"}
        return {
            "thread_id": thread_id, "identity": identity, "plan_id": plan.id,
            "a": task_a.id, "b": task_b.id, "labels": {**ids, thread_id: "<thread>"},
            "params": close_params(db, "request_review", task_a.id),
        }
    finally:
        db.close()


def _close_arguments(scene: dict, *, named: bool) -> dict:
    params = dict(scene["params"])
    if named:
        params["next_node_id"] = scene["b"]
    return {
        "plan_id": scene["plan_id"], "mutation_id": "close-a",
        "operation": "request_review", "params": params,
    }


def _mapped_block(text: str, labels: dict[str, str]) -> str:
    """Return the serialized block with generated ids replaced by scene labels."""
    mapped = serialized(json.loads(text)["next"])
    for real, label in labels.items():
        mapped = mapped.replace(real, label)
    return mapped


async def _native(scene: dict, arguments: dict) -> tuple[str, str]:
    from tests.plan_execution_helpers import execute_as

    registry = CapturingRegistry()
    registry.register(PlanUpdateTool())
    raw = await execute_as(
        registry, scene["identity"], scene["thread_id"], "plan_update", arguments,
    )
    return raw, registry.returned[-1]


def _assistant_step(scene: dict, call_id: str, arguments: dict) -> str:
    from subagents.child_thread import persist_assistant_step

    db = get_db()
    try:
        step_id = persist_assistant_step(
            scene["thread_id"], content="",
            tool_uses=[{"id": call_id, "name": "plan_update", "arguments": arguments}],
            turn_id=scene["identity"].turn_id, db=db,
        )
        db.commit()
        return step_id
    finally:
        db.close()


async def _claude_code(scene: dict, arguments: dict) -> tuple[str, str]:
    from subagents.harness_tool_bridge import execute_tool

    registry = CapturingRegistry()
    registry.register(PlanUpdateTool())
    step_id = _assistant_step(scene, "call-claude", arguments)
    result = await execute_tool(
        registry, thread_id=scene["thread_id"], turn_identity=scene["identity"],
        invocation_id="invocation-claude", provider_tool_call_id="call-claude",
        assistant_step_id=step_id, name="plan_update", arguments=arguments,
    )
    assert result.delivery.content == registry.returned[-1]
    return str(result), registry.returned[-1]


async def _codex(scene: dict, arguments: dict, monkeypatch, call_id: str = "call-codex"):
    from codex_runtime_fixtures import declare_codex_tool_step
    from subagents.codex_app_server import CodexAppServerSession

    monkeypatch.setattr(
        "subagents.harness_tool_bridge.settle_codex_tool_result", AsyncMock(return_value=None),
    )
    registry = CapturingRegistry()
    registry.register(PlanUpdateTool())
    session = CodexAppServerSession(
        binary="codex", task=None, tool_registry=registry,
        corsia_thread_id=scene["thread_id"], corsia_turn_identity=scene["identity"],
    )
    declare_codex_tool_step(session, call_id, name="plan_update", arguments=arguments)
    response, error = await session._handle_dynamic_tool_request({
        "tool": "plan_update", "arguments": arguments, "callId": call_id,
    })
    assert error is None and response["success"] is True
    [item] = response["contentItems"]
    return item["text"], registry.returned[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["without-named", "named"])
async def test_native_claude_code_and_codex_deliver_the_identical_next_block(
    isolated_db, monkeypatch, named,
):
    scenes = {name: _scene(mk_session(f"parity-{name}")) for name in ("native", "claude", "codex")}

    native_text, native_raw = await _native(scenes["native"], _close_arguments(scenes["native"], named=named))
    claude_text, claude_raw = await _claude_code(scenes["claude"], _close_arguments(scenes["claude"], named=named))
    codex_text, codex_raw = await _codex(scenes["codex"], _close_arguments(scenes["codex"], named=named), monkeypatch)

    assert native_text == native_raw
    assert claude_text == with_invocation_reference(claude_raw, "invocation-claude")
    assert codex_text == with_invocation_reference(codex_raw, "call-codex")
    blocks = {
        "native": _mapped_block(native_raw, scenes["native"]["labels"]),
        "claude": _mapped_block(claude_raw, scenes["claude"]["labels"]),
        "codex": _mapped_block(codex_raw, scenes["codex"]["labels"]),
    }
    assert blocks["native"] == blocks["claude"] == blocks["codex"]
    assert_block_shape(json.loads(native_raw)["next"], named=named)
    assert '"named"' in blocks["native"] if named else '"named"' not in blocks["native"]


@pytest.mark.asyncio
async def test_a_codex_close_without_next_node_id_is_actionable_from_field_names_alone(
    isolated_db, monkeypatch,
):
    scene = _scene(mk_session("codex-schema-without-next"))

    text, raw = await _codex(scene, _close_arguments(scene, named=False), monkeypatch)

    block = json.loads(raw)["next"]
    assert text == with_invocation_reference(raw, "call-codex")
    assert "named" not in block
    assert (block["selected"]["how"], block["selected"]["title"]) == ("auto_selected", "Queued work")
    assert block["selected"]["acceptance_criteria"] == "Queued work is verified"
    listed_b = block["open_tasks"][0]
    assert (listed_b["id"], listed_b["state"], listed_b["wait"]) == (scene["b"], "pending", None)
    db = get_db()
    try:
        assert listed_b["revision"] == service._node_row(db, scene["b"])["revision"]
    finally:
        db.close()
    assert block["open_tasks_listed"] == "2 of 2 other open tasks"


@pytest.mark.asyncio
async def test_the_next_block_stays_whole_in_the_harness_preview_of_a_large_close(isolated_db):
    scene = _scene(mk_session("preview-large-close"), criteria="Large proof body. " * 1200)

    text, raw = await _claude_code(scene, _close_arguments(scene, named=True))

    block = json.loads(raw)["next"]
    assert block["named"]["started"] is True
    assert len(raw) > 20_000
    assert "CORSIA_TOOL_RESULT_PREVIEW" in text
    assert '"next": ' + serialized(block) in text
