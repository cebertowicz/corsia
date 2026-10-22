"""Harness tool identity is task-local under concurrent execution."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from tools.base import Tool
from tools.registry import ToolRegistry


class _CaptureTool(Tool):
    def __init__(self):
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "capture"

    @property
    def description(self) -> str:
        return "Capture concurrent execution identity."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        if kwargs["mode"] == "slow":
            self.slow_started.set()
            await self.release_slow.wait()
        self.calls.append(kwargs)
        return f"{kwargs['mode']}-done"


@pytest.mark.asyncio
async def test_concurrent_harness_calls_keep_exact_task_local_identity(
    isolated_db,
):
    from infra import state
    from subagents import harness_tool_bridge

    db = sqlite3.connect(isolated_db)
    db.executemany(
        "INSERT INTO threads "
        "(id,user_id,model,project_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("slow-thread", "slow-user", "test/model", "slow-project", "t", "t"),
            ("fast-thread", "fast-user", "test/model", "fast-project", "t", "t"),
        ],
    )
    db.commit()
    db.close()

    from core.turn_core import persist_turn_start
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from subagents.child_thread import persist_assistant_step

    def _seed_call(thread: str, mode: str):
        turn_db = get_db()
        turn_id = f"{mode}-turn"
        admission = admit_input(
            thread,
            role="user",
            content=mode,
            intent="apply_now",
            message_id=f"{mode}-input",
            turn_id=turn_id,
            boot_epoch="boot",
            admitted_at="t",
            db=turn_db,
        )
        assert admission.batch is not None
        identity = persist_turn_start(
            turn_id, thread, "test/model", mode, db=turn_db,
        )
        step_id = persist_assistant_step(
            thread,
            content="",
            tool_uses=[{
                "id": f"{mode}-call", "name": "capture",
                "arguments": {"mode": mode},
            }],
            turn_id=identity.turn_id,
            db=turn_db,
        )
        turn_db.commit()
        turn_db.close()
        return identity, step_id

    slow_identity, slow_step = _seed_call("slow-thread", "slow")
    fast_identity, fast_step = _seed_call("fast-thread", "fast")

    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)

    slow_task = asyncio.create_task(
        harness_tool_bridge.execute_tool(
            registry,
            thread_id="slow-thread",
            turn_identity=slow_identity,
            invocation_id="slow-invocation",
            provider_tool_call_id="slow-call",
            assistant_step_id=slow_step,
            name="capture",
            arguments={"mode": "slow"},
        )
    )
    await tool.slow_started.wait()

    fast_result = await harness_tool_bridge.execute_tool(
        registry,
        thread_id="fast-thread",
        turn_identity=fast_identity,
        invocation_id="fast-invocation",
        provider_tool_call_id="fast-call",
        assistant_step_id=fast_step,
        name="capture",
        arguments={"mode": "fast"},
    )
    assert fast_result.startswith("fast-done")
    tool.release_slow.set()
    slow_result = await slow_task
    assert slow_result.startswith("slow-done")
    assert tool.calls == [
        {
            "mode": "fast",
            "_thread_id": "fast-thread",
            "_thread_project_id": "fast-project",
            "_host_access_turn_identity": fast_identity,
        },
        {
            "mode": "slow",
            "_thread_id": "slow-thread",
            "_thread_project_id": "slow-project",
            "_host_access_turn_identity": slow_identity,
        },
    ]
    assert state._current_thread_id.get() is None
    assert not hasattr(state, "_tool_context")
    assert not hasattr(harness_tool_bridge, "_active_tool_calls")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["persistence", "identity", "projection"])
async def test_completed_harness_effect_survives_infrastructure_failure(
    isolated_db, monkeypatch, fault,
):
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db
    from subagents import harness_tool_bridge as bridge
    from subagents.child_thread import persist_assistant_step
    from tests.plan_execution_helpers import ProbeTool, registry_of, selected_plan
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    selected_plan(db, "thread-1")
    tool = ProbeTool(result="completed mutation bytes")
    step = persist_assistant_step("thread-1", content="", tool_uses=[{
        "id": "call", "name": tool.name, "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()

    def failed(*args, **kwargs):
        raise ToolResultBindingError("api_key=synthetic-private-cause")

    async def async_failed(*args, **kwargs):
        failed()

    if fault == "persistence":
        monkeypatch.setattr(bridge, "_persist_delivery_off_loop", async_failed)
    elif fault == "projection":
        monkeypatch.setattr(bridge, "_media_delivery", async_failed)
    with pytest.raises(CompletedToolFailure) as raised:
        await bridge.execute_tool(
            registry_of(tool), thread_id="thread-1", turn_identity=identity,
            invocation_id="invocation", provider_tool_call_id="call",
            assistant_step_id="absent-step" if fault == "identity" else step,
            name=tool.name, arguments={},
        )
    [completed] = raised.value.completed
    assert completed.result == "completed mutation bytes"
    assert (completed.thread_id, completed.turn_id, completed.invocation_id,
            completed.provider_tool_call_id) == ("thread-1", identity.turn_id, "invocation", "call")
    assert completed.assistant_step_id == ("absent-step" if fault == "identity" else step)
    rows = db.execute("SELECT id,content FROM messages WHERE role='tool'").fetchall()
    assert [row["content"] for row in rows] == ([tool.result] if fault == "projection" else [])
    assert (completed.canonical.message_id if completed.canonical else None) == (rows[0]["id"] if rows else None)
    assert tool.calls == 1
    assert not isinstance(raised.value, ToolResultBindingError)
    assert "synthetic-private-cause" not in str(raised.value)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, RuntimeError])
async def test_a_failed_dispatch_settlement_does_not_fail_a_completed_harness_call(
    isolated_db, monkeypatch, error_type,
):
    """The completed call is delivered; its row closes at Turn-terminal settlement."""
    from infra.database import get_db
    from plans import service, tool_dispatches
    from subagents import harness_tool_bridge as bridge
    from subagents.child_thread import persist_assistant_step
    from tests.plan_execution_helpers import ProbeTool, dispatch_rows, registry_of, selected_plan
    from tests.test_cc_tool_result_echo_binding import _seed_thread

    db = get_db()
    identity = _seed_thread(db)
    selected_plan(db, "thread-1")
    tool = ProbeTool(result="completed mutation bytes")
    step = persist_assistant_step("thread-1", content="", tool_uses=[{
        "id": "call", "name": tool.name, "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()

    def failed(*args, **kwargs):
        raise error_type("dispatch settlement unavailable")

    monkeypatch.setattr("plans.public.settle_tool_dispatch", failed)
    delivered = await bridge.execute_tool(
        registry_of(tool), thread_id="thread-1", turn_identity=identity,
        invocation_id="invocation", provider_tool_call_id="call",
        assistant_step_id=step, name=tool.name, arguments={},
    )

    assert "completed mutation bytes" in delivered
    assert tool.calls == 1
    rows = db.execute("SELECT content FROM messages WHERE role='tool'").fetchall()
    assert [row["content"] for row in rows] == [tool.result]
    [dispatch] = dispatch_rows(db)
    assert dispatch["settled_at"] is None
    with service._write(db):
        tool_dispatches.settle_turn(identity.turn_id, db=db)
    [closed] = dispatch_rows(db)
    assert closed["outcome"] == "unknown"
    db.close()


@pytest.mark.asyncio
async def test_checkpoint_media_projection_failure_preserves_private_effect(
    isolated_db, monkeypatch,
):
    from core.media_reads import MediaToolResult
    from infra.database import get_db
    from subagents.harness_tool_bridge import execute_tool
    from tests.plan_execution_helpers import ProbeTool, registry_of
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    reference = object()
    tool = ProbeTool(result=MediaToolResult("private media result", reference))

    def failed(*args, **kwargs):
        raise RuntimeError("private media projection failed")

    monkeypatch.setattr("core.media_delivery.tool_result_media_for_reference", failed)
    with pytest.raises(CompletedToolFailure) as raised:
        await execute_tool(registry_of(tool), thread_id="thread-1", turn_identity=identity,
                           invocation_id="cp-call", provider_tool_call_id="cp-call",
                           fork_id="checkpoint", name=tool.name, arguments={})
    [completed] = raised.value.completed
    assert completed.result == "private media result" and completed.result.media is reference
    assert completed.fork_id == "checkpoint" and completed.invocation_id == "cp-call"
    assert completed.canonical is None
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0] == 0
    assert tool.calls == 1
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["turn", "provider", "arguments", "checkpoint"])
async def test_completed_failure_refuses_outer_identity_contradiction(conflict):
    """Outer adapters may fill missing identity, never rebind known identity."""
    from core.turn_core import TurnIdentity
    from subagents.harness_tool_bridge import execute_tool
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    producer = CompletedToolCall(
        name="capture",
        result="completed producer result",
        arguments={"source": "producer"} if conflict == "arguments" else {"source": "outer"},
        thread_id="thread-1",
        turn_id="producer-turn" if conflict == "turn" else "outer-turn",
        invocation_id="invocation",
        provider_tool_call_id="producer-call" if conflict == "provider" else "outer-call",
        fork_id="producer-checkpoint" if conflict == "checkpoint" else None,
    )

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, _name, _arguments):
            raise CompletedToolFailure((producer,))

    with pytest.raises(CompletedToolFailure) as raised:
        await execute_tool(
            Registry(), thread_id="thread-1", turn_identity=TurnIdentity("outer-turn", 1),
            invocation_id="invocation", provider_tool_call_id="outer-call",
            fork_id="outer-checkpoint" if conflict == "checkpoint" else None,
            name="capture", arguments={"source": "outer"},
        )

    assert raised.value.completed == (producer,)
    assert "producer" not in str(raised.value)
