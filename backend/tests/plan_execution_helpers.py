"""Registry probes, Turn scaffolding and execution-state readers for Plans tests."""

from __future__ import annotations

import asyncio
from typing import Any

from core.turn_core import TurnIdentity
from infra import state
from plans import admission, service
from plans.boundary import PlanActor
from tools.base import Tool
from tools.registry import ToolRegistry

#: The registry's existing suffix for a real error result.
ERROR_HINT = "\n\n[Analyze the error above and try a different approach.]"


class ProbeTool(Tool):
    """A registry-visible tool that records every execution it receives."""

    def __init__(self, name: str = "uncataloged_probe", *, result: str = "ok") -> None:
        self._name = name
        self._result = result
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Record one deterministic execution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def result(self) -> str:
        """Return the exact text this probe produces."""
        return self._result

    async def execute(self, **_kwargs: Any) -> str:
        self.calls += 1
        return self._result


class BlockingProbe(ProbeTool):
    """A probe whose started call stays in flight until the test releases it."""

    def __init__(self, name: str = "blocking_probe", *, result: str = "ok") -> None:
        super().__init__(name, result=result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, **_kwargs: Any) -> str:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self._result


class SchemaProbeTool(ProbeTool):
    """A probe with a closed parameter schema, for names whose native layers read params."""

    def __init__(
        self,
        name: str,
        properties: dict[str, Any],
        *,
        required: tuple[str, ...] = (),
        result: str = "ok",
    ) -> None:
        super().__init__(name, result=result)
        self._properties = dict(properties)
        self._required = list(required)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self._properties,
            "required": self._required,
            "additionalProperties": False,
        }


def delegate_send_probe(result: str = "ok") -> SchemaProbeTool:
    """Return a probe shaped like ``delegate_send`` without sending anything."""
    return SchemaProbeTool(
        "delegate_send",
        {
            "thread_id": {"type": "string"},
            "message": {"type": "string"},
            "intent": {"type": ["string", "null"]},
        },
        required=("thread_id", "message"),
        result=result,
    )


def bash_probe(result: str = "ok") -> SchemaProbeTool:
    """Return a probe named ``bash`` that the host gate classifies but never runs."""
    return SchemaProbeTool(
        "bash", {"command": {"type": "string"}}, required=("command",), result=result,
    )


def read_file_probe(result: str = "ok") -> SchemaProbeTool:
    """Return a probe named ``read_file``; a workspace-relative path is an ordinary read."""
    return SchemaProbeTool(
        "read_file", {"path": {"type": "string"}}, required=("path",), result=result,
    )


def registry_of(*tools: Tool) -> ToolRegistry:
    """Return a registry holding exactly these tools."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def open_turn(db, thread_id: str, turn_id: str = "turn-1") -> TurnIdentity:
    """Open one running Turn for the executor and return its identity."""
    cursor = db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','t','test')""",
        (turn_id, thread_id),
    )
    db.commit()
    return TurnIdentity(turn_id, int(cursor.lastrowid))


def finish_turn(db, turn_id: str) -> None:
    """Mark one Turn completed, so it is no longer live for its thread."""
    db.execute(
        "UPDATE active_turns SET status='completed',ended_at='done' WHERE id=?",
        (turn_id,),
    )
    db.commit()


def add_task(db, plan_id: str, title: str, **kwargs):
    """Add one DOD-bearing task to an existing plan."""
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is verified", db=db, **kwargs,
    )


def agent_pause(db, node_id: str, reason: str, *, thread_id: str = "owner") -> dict:
    """An agent's pause hold, placed by the runtime writer, committed.

    An agent can no longer pause (``plans.scheduled_wait``); a hold like this
    is one it placed before that, still its own to resume until the schema
    step schedules it. Returns the task's admission result, as the boundary did.
    """
    from plans import holds

    result = holds.pause(
        node_id, reason, expected_revision=service._node_row(db, node_id)["revision"],
        actor_id=thread_id, provenance="test", db=db,
    )
    db.commit()
    return result


def selected_plan(db, thread_id: str):
    """Return the executor's plan with one activated, selected task."""
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Selected work",
        acceptance_criteria="The shared registry check passes", db=db,
    )
    selected = admission.activate(
        node.id, expected_revision=node.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()
    return plan, selected


def thread_actor(thread_id: str) -> PlanActor:
    """Return the plan actor matching a ``mk_session`` thread row."""
    return PlanActor(thread_id, "user-1", None)


def dispatch_rows(db) -> list:
    """Return every recorded tool dispatch in start order."""
    return db.execute(
        "SELECT * FROM plan_tool_dispatches ORDER BY started_at,id",
    ).fetchall()


def open_attempt_ids(db) -> set[str]:
    """Return the ids of every attempt still holding an executor slot."""
    rows = db.execute(
        "SELECT id FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchall()
    return {str(row["id"]) for row in rows}


def turn_accounting(db, turn_id: str) -> tuple:
    """Return the Turn's plan accounting snapshot columns."""
    row = db.execute(
        """SELECT plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
                  plan_usage_role
           FROM active_turns WHERE id=?""",
        (turn_id,),
    ).fetchone()
    return tuple(row)


async def execute_as(registry, identity, thread_id, name, params=None):
    """Run one registry tool inside the executor's thread and Turn context."""
    thread_token = state._current_thread_id.set(thread_id)
    turn_token = state._current_turn_identity.set(identity)
    try:
        return await registry.execute(name, params or {})
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)


async def start_blocking_call(registry, identity, thread_id, probe: BlockingProbe):
    """Start one registry call and return its task once the tool body is running.

    If the call never reaches the tool (a boundary refused it), the assertion
    names the result that call returned instead of timing out silently.
    """
    call = asyncio.create_task(execute_as(registry, identity, thread_id, probe.name))
    started = asyncio.create_task(probe.started.wait())
    try:
        await asyncio.wait({call, started}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    finally:
        started.cancel()
    assert probe.started.is_set(), call.result() if call.done() else "call never started"
    return call


async def finish_blocking_call(probe: BlockingProbe, call) -> str:
    """Let one blocked call return and wait for the registry to settle it."""
    probe.release.set()
    return await asyncio.wait_for(call, timeout=5)


async def abandon_blocking_call(probe: BlockingProbe, call) -> None:
    """Release a blocked call during test cleanup, whatever state it reached."""
    probe.release.set()
    await asyncio.gather(call, return_exceptions=True)
