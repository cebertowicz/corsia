"""A non-plan tool that mutates the plan hands work on inside its own call.

The registry records every started call under the executor's valid selection,
except Plans' own tools (tool catalog category `plans`). The probe here is
deliberately not one of them: it is a tool that can carry an external effect
and also changes the plan from inside its call. When that call archives the
selected task or changes its requirements, the mutation releases the attempt
and reconciles at once, though the call is still open (#2705), with no
`activate`, `start`, or next Turn: the successor is selected, or the edited
task is selected again. The call itself settles under the released attempt.

The real `plan_update` records no dispatch; that path is covered in `test_plan_tool_dispatch_handoff.py` and
`test_plan_tool_dispatch_plan_tools.py`.
"""

from __future__ import annotations

from typing import Any

from infra.database import get_db
from plans.boundary import mutate
from tests.plan_execution_helpers import ProbeTool, execute_as, open_turn
from tests.test_plan_reconcile_release import (
    ACTOR,
    attempt,
    open_attempt,
    queued_pair,
    revision,
    seed_threads,
)
from tools.registry import ToolRegistry


class _PlanMutatingTool(ProbeTool):
    """A non-plan tool whose call also mutates the plan through the boundary.

    Its name is not a catalog `plans` tool, so its call is recorded and its
    open dispatch defers the release its mutation asks for (unlike
    `plan_update`, which records no dispatch).
    """

    def __init__(self, plan_id: str, operation: str, params: dict[str, Any]) -> None:
        super().__init__("plan_mutating_probe", result="mutated")
        self._plan_id = plan_id
        self._operation = operation
        self._params = params

    async def execute(self, **_kwargs: Any) -> str:
        self.calls += 1
        connection = get_db()
        try:
            mutate(
                self._plan_id, self._operation, self._params, actor=ACTOR,
                provenance="test.plan_reconcile_registry",
                mutation_id=f"in-call-{self._operation}", db=connection,
            )
        finally:
            connection.close()
        return self._result


async def _mutate_inside_own_call(connection, plan_id: str, operation: str, params: dict):
    """Run the mutation inside one registry tool call by the plan owner's Turn."""
    identity = open_turn(connection, "owner")
    tool = _PlanMutatingTool(plan_id, operation, params)
    registry = ToolRegistry()
    registry.register(tool)
    result = await execute_as(registry, identity, "owner", tool.name)
    return result, tool


def _call_dispatch(connection, tool_name: str):
    return connection.execute(
        "SELECT * FROM plan_tool_dispatches WHERE tool_name=?", (tool_name,),
    ).fetchone()


async def test_archiving_the_selected_task_in_its_own_call_selects_the_next_task(
    isolated_db,
):
    connection = get_db()
    try:
        seed_threads(connection)
        plan, task_a, a_attempt, task_b = queued_pair(connection)

        result, tool = await _mutate_inside_own_call(
            connection, plan.id, "archive_node",
            {"node_id": task_a, "expected_revision": revision(connection, task_a)},
        )

        assert result == "mutated"
        assert tool.calls == 1
        dispatch = _call_dispatch(connection, tool.name)
        assert dispatch["attempt_id"] == a_attempt
        assert dispatch["outcome"] == "succeeded"
        assert connection.execute(
            "SELECT archived_at FROM plan_nodes WHERE id=?", (task_a,),
        ).fetchone()["archived_at"] is not None
        assert attempt(connection, a_attempt)["release_reason"] == "archived"
        assert open_attempt(connection, task_b)["executor_thread_id"] == "owner"
    finally:
        connection.close()


async def test_editing_the_selected_tasks_requirements_in_its_own_call_reselects_it(
    isolated_db,
):
    connection = get_db()
    try:
        seed_threads(connection)
        plan, task_a, stale_attempt, task_b = queued_pair(connection)

        result, tool = await _mutate_inside_own_call(
            connection, plan.id, "edit",
            {
                "node_id": task_a,
                "acceptance_criteria": "The revised requirement is verified",
                "expected_revision": revision(connection, task_a),
            },
        )

        assert result == "mutated"
        assert tool.calls == 1
        assert _call_dispatch(connection, tool.name)["attempt_id"] == stale_attempt
        assert attempt(connection, stale_attempt)["release_reason"] == "requirements_changed"
        reselected = open_attempt(connection, task_a)
        assert reselected is not None, "the edited task lost its slot"
        assert reselected["id"] != stale_attempt
        assert reselected["requirements_revision"] == 2
        assert open_attempt(connection, task_b) is None
    finally:
        connection.close()
