"""Which selected attempt a started tool call is recorded under.

`plan_node_attempts` alone selects work. A started call is recorded against the
executor's valid selection in its own thread plan, the only plan whose tasks
it runs, and never by `active_turns.plan_*`, which is write-once accounting
only. An attempt the executor still holds in another thread's plan never
counts, and recording never selects there: only the executor's own plan is
ever reconciled to fill an empty slot. Recording never binds or refuses.

`leave_attempt_in_plan` builds that legacy shape for the Turn-start, artifact
origin and query-cost suites.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, admission_attempts, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    delegate_send_probe,
    dispatch_rows,
    execute_as,
    open_turn,
    registry_of,
    selected_plan,
    turn_accounting,
)
from tools.host_access_gate import HostActionAuthorization


def leave_attempt_in_plan(db, plan_id: str, executor: str):
    """Leave ``executor`` an open attempt on a new active task in another thread's plan.

    Older data can still name another thread on a task row and hold that
    thread's attempt there. Only a plan's owner runs its tasks now, so such an
    attempt is never the executor's selection. Returns the attempt row.
    """
    node = add_task(db, plan_id, "Legacy delegated task")
    service.transition_node(node.id, "active", db=db)
    db.execute(
        "UPDATE plan_nodes SET executor_thread_id=?,owner_thread_id=? WHERE id=?",
        (executor, executor, node.id),
    )
    attempt = admission_attempts.insert(
        db, service._node_row(db, node.id), executor,
        actor_id=executor, provenance="test.legacy_attempt",
    )
    db.commit()
    return attempt


def _activate(db, node, actor_id: str) -> str:
    """Select one pending task and return its attempt id."""
    selected = admission.activate(
        node.id, expected_revision=node.revision, actor_id=actor_id,
        provenance="test", db=db,
    )
    db.commit()
    return selected["admission"]["selection"]["attempt_id"]


def _bind_turn_row(db, turn_id: str, attempt_id: str) -> None:
    """Point a Turn's accounting snapshot at one attempt, as stale state would."""
    attempt = db.execute(
        """SELECT a.plan_id,a.node_id,n.commitment_id FROM plan_node_attempts a
           JOIN plan_nodes n ON n.id=a.node_id WHERE a.id=?""",
        (attempt_id,),
    ).fetchone()
    db.execute(
        """UPDATE active_turns
           SET plan_id=?,plan_node_id=?,plan_attempt_id=?,plan_commitment_id=?,
               plan_usage_role='execution'
           WHERE id=?""",
        (attempt["plan_id"], attempt["node_id"], attempt_id,
         attempt["commitment_id"], turn_id),
    )
    db.commit()


def _only_dispatch(db):
    rows = dispatch_rows(db)
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_stale_turn_accounting_does_not_refuse_or_redirect_the_selection(
    isolated_db,
):
    """The Turn still names finished Task A while Task B is selected."""
    thread_id = mk_session("attr-stale-accounting")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    first = add_task(db, plan.id, "Finished first", order_key=0)
    second = add_task(db, plan.id, "Selected second", order_key=1)
    first_attempt = _activate(db, first, thread_id)
    service.transition_node(first.id, "failed", db=db)
    assert admission.release(first.id, "failed", db=db)
    db.commit()
    second_attempt = _activate(db, second, thread_id)
    identity = open_turn(db, thread_id)
    _bind_turn_row(db, identity.turn_id, first_attempt)
    accounting = turn_accounting(db, identity.turn_id)
    tool = ProbeTool(result="probe ran")

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "probe ran"
    assert tool.calls == 1
    dispatch = _only_dispatch(db)
    assert dispatch["attempt_id"] == second_attempt
    assert dispatch["turn_id"] == identity.turn_id
    assert dispatch["outcome"] == "succeeded"
    assert turn_accounting(db, identity.turn_id) == accounting
    db.close()


@pytest.mark.asyncio
async def test_only_the_callers_own_plan_selection_records_its_calls(isolated_db):
    """An attempt the caller still holds in its parent's plan never counts.

    The child's Turn accounting even names that older attempt. The child's call
    runs unrecorded until the child selects work in its own plan, and is then
    recorded under that selection. Turn accounting never changes.
    """
    parent_id = mk_session("attr-own-parent")
    child_id = mk_session("attr-own-child", spawned_by=parent_id)
    db = get_db()
    parent_plan = service.ensure_plan("thread", parent_id, db=db)
    legacy = leave_attempt_in_plan(db, parent_plan.id, child_id)
    identity = open_turn(db, child_id)
    _bind_turn_row(db, identity.turn_id, legacy["id"])
    accounting = turn_accounting(db, identity.turn_id)
    tool = ProbeTool()
    registry = registry_of(tool)

    assert await execute_as(registry, identity, child_id, tool.name) == "ok"
    assert dispatch_rows(db) == []

    child_plan = service.ensure_plan("thread", child_id, db=db)
    own_attempt = _activate(db, add_task(db, child_plan.id, "Child-owned"), child_id)

    assert await execute_as(registry, identity, child_id, tool.name) == "ok"

    assert tool.calls == 2
    assert _only_dispatch(db)["attempt_id"] == own_attempt
    assert turn_accounting(db, identity.turn_id) == accounting
    db.close()


#: Continuity, restart and stop/release tools: control calls are recorded like
#: any other started call. Plans' own tools (tool catalog category ``plans``)
#: record no dispatch; `test_plan_tool_dispatch_plan_tools.py` covers them.
_CONTROL_AND_STOP_NAMES = (
    "continuity_edit", "restart_resume", "managed_process_stop", "browser_close",
    "automation_cancel", "release_host_access", "process_signal",
)

#: Names whose call starts only after a host-access lease review allows it.
_HOST_REVIEWED_NAMES = frozenset({"process_signal"})


async def _host_review_allows(**_kwargs):
    """Stand in for an allow verdict from host-access review under a lease."""
    return HostActionAuthorization()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _CONTROL_AND_STOP_NAMES)
async def test_control_and_stop_calls_are_recorded_under_the_selection(
    isolated_db, monkeypatch, name,
):
    """Control and stop calls are recorded under the selection like any started call.

    `process_signal` reaches its tool only after host access reviews a real
    process binding under a lease, so its host verdict is stubbed as allowed;
    the host boundary itself is covered by the host-access suites.
    """
    if name in _HOST_REVIEWED_NAMES:
        monkeypatch.setattr(
            "tools.host_access_gate.authorize_host_action", _host_review_allows,
        )
    thread_id = mk_session(f"attr-uniform-{name}")
    db = get_db()
    identity = open_turn(db, thread_id)
    _plan, selected = selected_plan(db, thread_id)
    tool = ProbeTool(name, result=f"{name} ran")

    assert await execute_as(registry_of(tool), identity, thread_id, name) == f"{name} ran"

    dispatch = _only_dispatch(db)
    assert dispatch["attempt_id"] == selected["admission"]["selection"]["attempt_id"]
    assert dispatch["tool_name"] == name
    assert dispatch["turn_id"] == identity.turn_id
    assert dispatch["outcome"] == "succeeded"
    assert dispatch["settled_at"] is not None
    db.close()


@pytest.mark.asyncio
async def test_a_completion_return_is_recorded_under_the_selection(isolated_db):
    """A child's completion return is an ordinary started call."""
    parent_id = mk_session("attr-completion-parent")
    child_id = mk_session("attr-completion-child", spawned_by=parent_id)
    db = get_db()
    identity = open_turn(db, child_id)
    _plan, selected = selected_plan(db, child_id)
    tool = delegate_send_probe(result="completion accepted")

    result = await execute_as(
        registry_of(tool), identity, child_id, tool.name,
        {"thread_id": parent_id, "message": "finished", "intent": "completion"},
    )

    assert result == "completion accepted"
    dispatch = _only_dispatch(db)
    assert dispatch["attempt_id"] == selected["admission"]["selection"]["attempt_id"]
    assert dispatch["tool_name"] == "delegate_send"
    db.close()


@pytest.mark.asyncio
async def test_recording_never_binds_turn_accounting(isolated_db):
    """The dispatch recorder writes its row and nothing on the Turn."""
    thread_id = mk_session("attr-never-binds")
    db = get_db()
    _plan, selected = selected_plan(db, thread_id)
    identity = open_turn(db, thread_id)
    tool = ProbeTool()

    assert await execute_as(registry_of(tool), identity, thread_id, tool.name) == "ok"

    assert _only_dispatch(db)["attempt_id"] == (
        selected["admission"]["selection"]["attempt_id"]
    )
    assert turn_accounting(db, identity.turn_id) == (None, None, None, None, None)
    db.close()
