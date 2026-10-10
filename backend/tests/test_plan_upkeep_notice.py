"""A Turn's first tool result carries one short line about the thread's plan task.

The line names a selected task as active, a task the agent set aside with its
real hold, or says nothing is selected while open tasks wait. Results of the
Plans tools, threads without an active plan, and every result but the one
answering the Turn's first call carry none. The line stays within its byte
bound and sits after the result, before the harness echo marker.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.plan_notice import first_work_call, with_notice
from core.tool_result_invocation import with_invocation_reference
from plans import service
from plans.upkeep_notice import NOTICE_MAX_BYTES, notice_for
from prompts.plan_tools import UPKEEP_ADD_ROUTE
from tests.plan_close_next_helpers import owner_plan, pause, select, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture
from tools.base import Tool
from tools.registry import ToolRegistry


class _FixtureDb:
    """Keep the fixture connection open when the notice closes its own handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self) -> None:
        return None


class _ImageTool(Tool):
    """A tool whose result the delivery layer answers with media."""

    @property
    def name(self) -> str:
        return "read_image"

    @property
    def description(self) -> str:
        return "Read one image."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        return '{"kind":"image"}'


def test_a_selected_task_is_named_as_active(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    select(db, plan.id, brief.id)

    line = notice_for(db, "owner", "bash")

    assert line == (
        "[CORSIA plan notice] Current task: Draft the brief — active. Update it if "
        "it is finished, blocked, or you have moved to different work."
    )


def test_a_task_set_aside_on_a_hold_is_named_with_that_hold(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    select(db, plan.id, brief.id)
    pause(db, plan.id, brief.id, "Waiting on legal review")

    line = notice_for(db, "owner", "read_file")

    assert line is not None
    assert "Current task: Draft the brief — paused: Waiting on legal review." in line
    assert "Leave it held while that still applies" in line


def test_nothing_selected_names_the_startable_task_and_the_start_call(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")

    assert notice_for(db, "owner", "bash") == (
        f"[CORSIA plan notice] No plan task is selected. Startable: Draft the brief "
        f"({brief.id}). Start one with plan_update start node_id={brief.id} "
        f"expected_revision={brief.revision} when you begin it, or update the plan "
        "if the work changed."
    )


def test_the_start_call_carries_every_field_start_requires(db):
    from plans.boundary_fields import OPERATION_REQUIRED_FIELDS

    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    values = {"node_id": brief.id, "expected_revision": str(brief.revision)}

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert set(values) == OPERATION_REQUIRED_FIELDS["start"]
    assert "plan_update start " + " ".join(f"{k}={v}" for k, v in values.items()) in line


def test_the_start_call_names_the_revision_of_the_first_task_not_the_second(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "First")
    second = task(db, plan.id, "Second")
    db.execute("UPDATE plan_nodes SET revision=revision+6 WHERE id=?", (second.id,))
    db.commit()

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert f"node_id={first.id} expected_revision={first.revision} " in line


def test_nothing_selected_names_the_top_two_startable_tasks_in_plan_order(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "First")
    second = task(db, plan.id, "Second")
    third = task(db, plan.id, "Third")

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert f"Startable: First ({first.id}); Second ({second.id})." in line
    assert f"plan_update start node_id={first.id}" in line
    assert third.id not in line
    assert len(line.encode("utf-8")) <= NOTICE_MAX_BYTES


def test_the_start_line_keeps_both_ids_and_the_call_within_the_byte_bound(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "\U0001F4E6" * 60)
    second = task(db, plan.id, "\U0001F4E6" * 60)

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert len(line.encode("utf-8")) <= NOTICE_MAX_BYTES
    assert f"({first.id})" in line and f"({second.id})" in line
    assert f"plan_update start node_id={first.id} expected_revision={first.revision}" in line


def test_open_tasks_that_are_all_waiting_are_named_before_the_add_route(db):
    plan = owner_plan(db)
    awaiting = task(db, plan.id, "Await legal")
    pause(db, plan.id, awaiting.id, "Legal is reviewing")

    line = notice_for(db, "owner", "bash")

    assert line == (
        f"[CORSIA plan notice] No plan task is selected. Waiting: Await legal "
        f"({awaiting.id}). If this work belongs to one, update that task; "
        "otherwise add it with plan_update add, then start it."
    )
    assert "plan_update start" not in line
    assert "plan_update add" in UPKEEP_ADD_ROUTE
    assert len(UPKEEP_ADD_ROUTE.encode("utf-8")) <= NOTICE_MAX_BYTES


def test_the_waiting_line_keeps_both_ids_and_the_add_route_within_the_byte_bound(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "\U0001F4E6" * 60)
    second = task(db, plan.id, "\U0001F4E6" * 60)
    pause(db, plan.id, first.id, "Legal is reviewing")
    pause(db, plan.id, second.id, "Legal is reviewing")

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert len(line.encode("utf-8")) <= NOTICE_MAX_BYTES
    assert f"({first.id})" in line and f"({second.id})" in line
    assert line.endswith("otherwise add it with plan_update add, then start it.")


def test_a_task_awaiting_review_is_named_not_met_with_the_add_route(db):
    plan = owner_plan(db)
    step = task(db, plan.id, "Delegated step")
    service.transition_node(step.id, "review_requested", db=db)
    db.commit()

    line = notice_for(db, "owner", "bash")

    assert line == (
        f"[CORSIA plan notice] No plan task is selected. Waiting: Delegated step "
        f"({step.id}). If this work belongs to one, update that task; "
        "otherwise add it with plan_update add, then start it."
    )


def test_a_user_assigned_open_task_is_named_not_met_with_the_add_route(db):
    plan = owner_plan(db)
    approval = task(db, plan.id, "Approve the copy", assignee="user")

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert line.startswith("[CORSIA plan notice] No plan task is selected. Waiting: Approve")
    assert f"({approval.id})" in line
    assert line != UPKEEP_ADD_ROUTE


def test_unsettled_tasks_beyond_the_cap_are_not_all_named(db):
    plan = owner_plan(db)
    steps = [task(db, plan.id, f"Step {n}") for n in range(3)]
    for step in steps:
        service.transition_node(step.id, "review_requested", db=db)
    db.commit()

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert steps[0].id in line and steps[1].id in line and steps[2].id not in line


def test_only_settled_tasks_get_the_add_route(db):
    plan = owner_plan(db)
    done = task(db, plan.id, "Failed")
    gone = task(db, plan.id, "Dropped")
    service.transition_node(done.id, "failed", db=db)
    service.transition_node(gone.id, "closed", db=db)
    db.commit()

    assert notice_for(db, "owner", "bash") == UPKEEP_ADD_ROUTE


def test_an_active_plan_with_no_open_task_gets_the_add_route(db):
    owner_plan(db)

    assert notice_for(db, "owner", "bash") == UPKEEP_ADD_ROUTE


def test_a_paused_plan_carries_no_notice(db):
    plan = owner_plan(db)
    task(db, plan.id, "Draft the brief")
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (plan.id,))
    db.commit()

    assert notice_for(db, "owner", "bash") is None


def test_plans_tools_and_threads_without_a_plan_carry_no_notice(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    select(db, plan.id, brief.id)

    assert notice_for(db, "owner", "plan_update") is None
    assert notice_for(db, "owner", "mcp__corsia__plan_read") is None
    assert notice_for(db, "owner", "plan_decision") is None
    assert notice_for(db, "owner", "plan_ensure") is None
    assert notice_for(db, "no-plan-thread", "bash") is None


def test_the_notice_stays_within_its_byte_bound(db):
    plan = owner_plan(db)
    # Titles stop at 60 characters, but four-byte characters still overrun the
    # notice's 96-byte title cut.
    long_task = task(db, plan.id, "\U0001F4E6" * 60)
    select(db, plan.id, long_task.id)

    line = notice_for(db, "owner", "bash")

    assert line is not None
    assert len(line.encode("utf-8")) <= NOTICE_MAX_BYTES
    assert line.endswith("or you have moved to different work.")


def test_the_turns_first_work_call_is_the_only_one_that_answers(db):
    def assistant(turn_id: str, seq: int, calls: list[str]) -> None:
        metadata = {"turn_id": turn_id, "llm_tool_calls": [{"id": call} for call in calls]}
        db.execute(
            """INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq)
               VALUES (?, 'owner', 'assistant', '', ?, 't', ?)""",
            (uuid.uuid4().hex, json.dumps(metadata), seq),
        )

    assistant("turn-1", 1, [])
    assistant("turn-1", 2, ["call-a", "call-b"])
    assistant("turn-1", 3, ["call-c"])
    assistant("turn-2", 4, ["call-d"])
    db.commit()

    assert first_work_call(db, "owner", "turn-1") == "call-a"
    assert first_work_call(db, "owner", "turn-2") == "call-d"
    assert first_work_call(db, "owner", "turn-3") is None


def _row(
    db, thread_id: str, role: str, seq: int, metadata: str,
) -> None:
    db.execute(
        """INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq)
           VALUES (?, ?, ?, '', ?, 't', ?)""",
        (uuid.uuid4().hex, thread_id, role, metadata, seq),
    )


def _declare(db, thread_id: str, turn_id: str, seq: int, calls: list[str]) -> None:
    metadata = {"turn_id": turn_id, "llm_tool_calls": [{"id": call} for call in calls]}
    _row(db, thread_id, "assistant", seq, json.dumps(metadata))


def test_earlier_turns_calls_are_never_the_current_turns_first(db):
    _declare(db, "owner", "turn-old", 1, ["old-a"])
    _declare(db, "owner", "turn-old", 2, ["old-b"])
    _declare(db, "owner", "turn-cur", 3, [])
    _declare(db, "owner", "turn-cur", 4, ["cur-a"])
    db.commit()

    assert first_work_call(db, "owner", "turn-cur") == "cur-a"
    assert first_work_call(db, "owner", "turn-old") == "old-a"


def test_a_call_arriving_in_a_later_row_is_found_after_empty_rows(db):
    _declare(db, "owner", "turn-cur", 10, [])
    _declare(db, "owner", "turn-cur", 11, [])
    _declare(db, "owner", "turn-cur", 14, ["late-a", "late-b"])
    db.commit()

    assert first_work_call(db, "owner", "turn-cur") == "late-a"


def test_the_first_call_is_chosen_by_seq_not_by_write_order(db):
    _declare(db, "owner", "turn-cur", 9, ["later-row"])
    _declare(db, "owner", "turn-cur", 5, ["earlier-row"])
    db.commit()

    assert first_work_call(db, "owner", "turn-cur") == "earlier-row"


def test_parallel_calls_in_the_first_batch_return_the_first(db):
    _declare(db, "owner", "turn-cur", 1, ["p-1", "p-2", "p-3"])
    _declare(db, "owner", "turn-cur", 2, ["p-4"])
    db.commit()

    assert first_work_call(db, "owner", "turn-cur") == "p-1"


def test_a_turn_without_declared_calls_has_no_first_call(db):
    _declare(db, "owner", "turn-other", 1, ["other-a"])
    _declare(db, "owner", "turn-cur", 2, [])
    _row(db, "owner", "assistant", 3, json.dumps({"turn_id": "turn-cur"}))
    db.commit()

    assert first_work_call(db, "owner", "turn-cur") is None
    assert first_work_call(db, "owner", "turn-absent") is None


def test_rows_at_the_turn_boundary_belong_only_to_their_own_turn(db):
    _declare(db, "owner", "turn-1", 1, ["t1-a"])
    _declare(db, "owner", "turn-2", 2, [])
    _declare(db, "owner", "turn-1", 3, ["t1-b"])
    _declare(db, "owner", "turn-2", 4, ["t2-a"])
    _declare(db, "owner", "turn-3", 5, ["t3-a"])
    db.commit()

    assert first_work_call(db, "owner", "turn-1") == "t1-a"
    assert first_work_call(db, "owner", "turn-2") == "t2-a"
    assert first_work_call(db, "owner", "turn-3") == "t3-a"


def test_only_this_threads_assistant_rows_with_readable_metadata_count(db):
    _declare(db, "worker", "turn-cur", 1, ["other-thread"])
    metadata = json.dumps({"turn_id": "turn-cur", "llm_tool_calls": [{"id": "user-row"}]})
    _row(db, "owner", "user", 2, metadata)
    _row(db, "owner", "assistant", 3, '{"turn_id": "turn-cur", "llm_tool_calls": [')
    _row(
        db, "owner", "assistant", 4,
        json.dumps({"turn_id": "turn-cur", "tool_calls": [{"id": "legacy-key"}]}),
    )
    db.commit()

    assert first_work_call(db, "owner", "turn-cur") == "legacy-key"


def test_the_read_seeks_the_turns_rows_not_the_whole_thread(db):
    _declare(db, "owner", "turn-old", 1, ["old-a"])
    _declare(db, "owner", "turn-cur", 2, ["cur-a"])
    db.commit()
    statements: list[tuple[str, tuple]] = []

    class _Recorder(_FixtureDb):
        def execute(self, sql, params=()):
            statements.append((sql, tuple(params)))
            return self._connection.execute(sql, params)

    assert first_work_call(_Recorder(db), "owner", "turn-cur") == "cur-a"

    (sql, params), = statements
    plan = "\n".join(
        str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    assert "SEARCH messages USING INDEX idx_messages_turn_calls" in plan
    assert "idx_messages_session_seq" not in plan


@pytest.mark.asyncio
async def test_a_media_result_carries_the_notice_after_its_placeholder(
    isolated_db, monkeypatch,
):
    from infra.database import get_db
    from subagents import harness_tool_bridge as bridge

    connection = get_db()
    seed_threads(connection)
    plan = owner_plan(connection)
    brief = task(connection, plan.id, "Draft the brief")
    select(connection, plan.id, brief.id)
    connection.execute(
        """INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq)
           VALUES (?, 'owner', 'assistant', '', ?, 't', 1)""",
        (
            uuid.uuid4().hex,
            json.dumps({"turn_id": "turn-1", "llm_tool_calls": [{"id": "call-1"}]}),
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        bridge, "_persist_delivery_off_loop",
        AsyncMock(return_value=SimpleNamespace(
            invocation_id="inv-1", assistant_step_id="step-1",
        )),
    )
    monkeypatch.setattr(
        bridge, "_media_delivery",
        AsyncMock(return_value=SimpleNamespace(text="[image: 1 attached]")),
    )
    registry = ToolRegistry()
    registry.register(_ImageTool())

    projected = await bridge.execute_tool(
        registry, thread_id="owner", turn_identity=SimpleNamespace(turn_id="turn-1"),
        invocation_id="inv-1", provider_tool_call_id="call-1",
        assistant_step_id="step-1", name="read_image", arguments={},
    )

    assert projected.startswith(
        "[image: 1 attached]\n\n[CORSIA plan notice] Current task: Draft the brief"
    )
    assert projected.endswith('{"invocation_id":"inv-1"}]')


def test_the_notice_sits_after_the_result_and_before_the_echo_marker():
    projected = with_invocation_reference(with_notice("result text", "NOTICE"), "inv-1")

    assert projected.startswith("result text\n\nNOTICE")
    assert projected.endswith('{"invocation_id":"inv-1"}]')
    assert with_notice("result text", None) == "result text"
