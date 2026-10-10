"""Release 2 journeys: the quiet child lifecycle end to end.

Each journey below drives the real Plans service, delegation harvest, the
plan-wake/notification writers and continuation routing (``HarvestSource``,
``plan_liveness``) exactly as production wires them; nothing in ``plans.*`` is
mocked. The only simulated boundary is the model/harness: a worker's Turn
outcome, a thread ending, or a user Stop are all reported the way a real
harness would, through the same functions those systems call
(``plan_liveness_terminal.on_thread_turn_complete``, ``delegation_child_lifecycle``,
``ws.stop_control._commit_stop_terminal``).

Many individual matrix rows already have focused unit coverage in
``test_child_parent_notice_{lifecycle,conditions,dead_child,working_child}.py``,
``test_plan_delegation_rollup.py``, ``test_delegation_tools.py`` and
``test_stop_receipts.py``; this module cites those rather than re-deriving
them; a prior HTTP/WS acceptance journey
(``shared/planning/ACCEPTANCE-JOURNEY-2026-09-12.md``) already established
"two children -> exactly two wakes", which the reading of Journey 1 below
follows.
"""

from __future__ import annotations

from automation import plan_liveness_terminal
import pytest

from automation import plan_liveness
from plans import delegation, mutations, service, wait_causes, watch_operations
from plans.projections import liveness_signal
from tests.delegation_fixtures import deleg_db  # noqa: F401
from tests.child_parent_notice_helpers import DEADLINE, db, delegated, wakes
from tests.plan_delegation_journey_helpers import (
    attach_child,
    link_artifact,
    wake_payload,
)
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]


# === Journey 1: happy path, two children ====================================


def test_journey1_two_children_each_return_with_one_full_evidence_wake(db):
    """Intermediate progress is quiet; each child's own return is its own wake.

    Confirms the reading of "exactly one wake, containing each child's step
    evidence" as one wake *per child* (not one wake merging two children):
    ``plan_wake.notify_returned`` addresses one delegated node at a time
    (``subject_id=node_id``), and the prior acceptance journey measured
    exactly this for two children (ACCEPTANCE-JOURNEY-2026-09-12.md: "two
    children -> exactly two wakes").
    """
    step_a, _plan_a, node_a = attach_child(db, "worker-a", title="Delegate A")
    step_b, _plan_b, node_b = attach_child(db, "worker-b", title="Delegate B")

    # Ordinary progress on both children is a digest only, never a wake.
    service.transition_node(node_a.id, "active", actor_id="worker-a", db=db)
    service.transition_node(node_b.id, "active", actor_id="worker-b", db=db)
    assert wakes(db) == []

    link_artifact(db, node_a.id, "artifact-a1", actor_id="worker-a")
    mutations.request_review(
        node_a.id,
        evidence="A's tests pass",
        artifact_ids=["artifact-a1"],
        expected_revision=service.get_node(node_a.id, db=db).revision,
        actor_id="worker-a",
        owner_thread_id="worker-a",
        provenance="test.journey",
        db=db,
    )
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step_a.id]
    after_a = wakes(db)
    assert len(after_a) == 1
    payload_a = wake_payload(after_a[0])["returned"]
    assert (
        payload_a["child_plan_id"]
        == service.ensure_plan("thread", "worker-a", db=db).id
    )
    assert payload_a["worker_thread_id"] == "worker-a"
    assert [s["id"] for s in payload_a["steps"]] == [node_a.id]
    assert payload_a["steps"][0]["evidence"] == "A's tests pass"
    assert payload_a["steps"][0]["artifact_ids"] == ["artifact-a1"]
    # Neither child B's quiet progress nor A's return told the parent about B.
    assert node_b.id not in [s["id"] for s in payload_a["steps"]]

    link_artifact(db, node_b.id, "artifact-b1", actor_id="worker-b")
    submit_review_evidence(node_b.id, db=db, actor_id="worker-b", evidence="B is done")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step_b.id]
    after_b = wakes(db)
    assert len(after_b) == 2
    payload_b = wake_payload(after_b[1])["returned"]
    assert payload_b["worker_thread_id"] == "worker-b"
    assert [s["id"] for s in payload_b["steps"]] == [node_b.id]

    # A's wake is untouched by B's later return (one wake per child, no merge).
    assert wake_payload(after_b[0])["returned"] == payload_a


def test_journey1_structured_childs_explicit_completion_is_refused(
    deleg_db, monkeypatch
):
    """A structured child's own send of intent="completion" adds no parent input.

    Mirrors ``test_delegation_tools.py::
    test_structured_child_explicit_completion_is_refused`` (same fixture,
    same call), which already proves the exact refusal message; this reuses
    that setup and additionally asserts no plan-wake row was appended.
    """
    import asyncio
    from unittest.mock import AsyncMock

    import plans.mutations as tool_mutations
    import plans.service as tool_service
    from tools.delegation import _send_explicit_completion

    def _sync_tool_db(monkeypatch):
        import tools.delegation as delegation_tools

        monkeypatch.setattr(
            delegation_tools,
            "run_in_db",
            AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k)),
        )

    _sync_tool_db(monkeypatch)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('parent-1','u-test','claude-code/opus','Parent','t','t')"
    )
    deleg_db.execute("UPDATE threads SET spawned_by='parent-1' WHERE id='sender-self'")
    deleg_db.commit()
    parent_plan = tool_service.ensure_plan("thread", "parent-1", db=deleg_db)
    node = tool_service.add_node(
        parent_plan.id,
        kind="task",
        title="Delegate",
        acceptance_criteria="Delegate DOD",
        spawn_kind="claude-code",
        db=deleg_db,
    )
    tool_service.transition_node(node.id, "active", db=deleg_db)
    tool_service.ensure_plan("thread", "sender-self", db=deleg_db)
    tool_mutations.attach_spawned_thread(node.id, "sender-self", db=deleg_db)
    deleg_db.commit()
    monkeypatch.setattr("tools.delegation.send_thread_input", AsyncMock())
    before_wakes = len(wakes(deleg_db, thread_id="parent-1"))

    result = asyncio.run(
        _send_explicit_completion("sender-self", "parent-1", "child-turn-1", "finished")
    )

    assert "already delivers your result to the parent" in result
    assert len(wakes(deleg_db, thread_id="parent-1")) == before_wakes
    assert tool_service.get_node(node.id, db=deleg_db).state == "active"
    # #282: no step to hold evidence; the Turn's last message is the return.
    assert "last message this turn is the return" in result
    assert "write nothing after it" in result


def test_journey1_refusal_for_a_worker_with_steps_names_step_evidence(
    deleg_db, monkeypatch
):
    """#282: only a planless worker is told to return by its final reply."""
    import asyncio
    from unittest.mock import AsyncMock

    import plans.mutations as tool_mutations
    import plans.service as tool_service
    import tools.delegation as delegation_tools
    from tools.delegation import _send_explicit_completion

    monkeypatch.setattr(
        delegation_tools, "run_in_db",
        AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k)),
    )
    monkeypatch.setattr("tools.delegation.send_thread_input", AsyncMock())
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('parent-1','u-test','claude-code/opus','Parent','t','t')"
    )
    deleg_db.execute("UPDATE threads SET spawned_by='parent-1' WHERE id='sender-self'")
    deleg_db.commit()
    parent_plan = tool_service.ensure_plan("thread", "parent-1", db=deleg_db)
    node = tool_service.add_node(
        parent_plan.id, kind="task", title="Delegate",
        acceptance_criteria="Delegate DOD", spawn_kind="claude-code", db=deleg_db,
    )
    tool_service.transition_node(node.id, "active", db=deleg_db)
    own = tool_service.ensure_plan("thread", "sender-self", db=deleg_db)
    tool_service.add_node(
        own.id, kind="task", title="Own step", acceptance_criteria="Done", db=deleg_db,
    )
    tool_mutations.attach_spawned_thread(node.id, "sender-self", db=deleg_db)
    deleg_db.commit()

    result = asyncio.run(
        _send_explicit_completion("sender-self", "parent-1", "child-turn-1", "finished")
    )

    assert "Put the result in your step's review evidence" in result
    assert "final reply" not in result


# === Journey 2: waits ========================================================


def test_journey2_watch_and_working_child_stay_quiet_across_liveness_cycles(db):
    """A child mid-turn with an armed watch never repeats a wake, T0 included."""
    step, plan, node = delegated(db)
    prerequisite = service.add_node(
        plan.id,
        kind="task",
        title="Moving prerequisite",
        acceptance_criteria="Prerequisite passes",
        db=db,
    )
    service.add_dependency(node.id, prerequisite.id, actor_id="worker", db=db)
    watched = service.add_node(
        plan.id,
        kind="task",
        title="Watched step",
        acceptance_criteria="CI passes",
        db=db,
    )
    service.transition_node(watched.id, "active", actor_id="worker", db=db)
    watch_operations.arm(
        plan.id,
        {
            "node_id": watched.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "ci.completed",
            "expected_revision": service.get_node(watched.id, db=db).revision,
        },
        _worker_actor(),
        db,
    )
    from plans import admission

    assert wait_causes.DELEGATED_CHILD in [
        c["reason_key"] for c in admission.project(step.id, db=db)["causes"]
    ]
    assert liveness_signal("parent", db=db).count == 0

    for _cycle in range(4):
        plan_liveness.on_turn_complete("parent", db=db)
        plan_liveness.on_turn_complete("worker", db=db)

    assert wakes(db) == []


def _worker_actor():
    from plans.boundary import PlanActor

    return PlanActor("worker", "user-1", "project-1")


def _admit_stop_input(main_db):
    from infra import state
    from infra.thread_driver import admit_input

    state._active_turns.clear()
    main_db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at,spawned_by) "
        "VALUES ('child-live','steve','claude-code/opus','t','t','main-1')"
    )
    main_db.commit()
    return admit_input(
        "main-1",
        role="user",
        content="Stop test",
        intent="apply_now",
        message_id="turn-stop:input",
        turn_id="turn-stop",
        boot_epoch="test-boot",
        admitted_at="t",
        db=main_db,
    )


def _start_stoppable_turn(main_db, batch):
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import persist_turn_start
    from infra import state
    from infra.input_writes import begin_batch_write, settle_batch_write

    begin_batch_write(
        batch,
        attempt_id="turn-stop:write",
        started_at="t",
        db=main_db,
    )
    settle_batch_write("turn-stop:write", outcome="accepted", db=main_db)
    identity = persist_turn_start(
        "turn-stop", "main-1", "claude-code/opus", "Stop test", db=main_db
    )
    main_db.commit()
    event = TurnCancelEvent()
    event.bind_turn_identity(identity)
    state._active_turns["main-1"] = event
    return identity


def _activate(plan, node, main_db, mutation_id):
    from plans import boundary

    boundary.mutate(
        plan.id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=boundary.PlanActor("main-1", "steve", None),
        provenance="test.journey2",
        mutation_id=mutation_id,
        db=main_db,
    )


def _add_working_delegation(main_db):
    plan = service.ensure_plan("thread", "main-1", db=main_db)
    stuck = service.add_node(
        plan.id,
        kind="task",
        title="Needs attention",
        acceptance_criteria="Someone looks at it",
        db=main_db,
    )
    _activate(plan, stuck, main_db, "activate-stuck")
    delegated_step = service.add_node(
        plan.id,
        kind="task",
        title="Delegated and working",
        acceptance_criteria="Worker finishes",
        spawn_kind="claude-code",
        db=main_db,
    )
    _activate(plan, delegated_step, main_db, "activate-delegated")
    mutations.attach_spawned_thread(delegated_step.id, "child-live", db=main_db)


@pytest.mark.asyncio
async def test_journey2_user_stop_cancels_the_armed_liveness_wake(main_db):
    """A Stop on a plan that also carries live delegation still cancels its wake.

    The cancel-on-stop mechanism itself is already proven by
    ``test_stop_receipts.py::test_stop_commit_cancels_the_plan_liveness_wake``;
    this repeats it with a working delegated child also present on the same
    plan, to show the two do not interfere.
    """
    from infra import state
    from ws.stop_control import _commit_stop_terminal

    admission = _admit_stop_input(main_db)
    assert admission.batch is not None
    identity = _start_stoppable_turn(main_db, admission.batch)
    _add_working_delegation(main_db)
    wake = plan_liveness.on_turn_complete("main-1", db=main_db)
    main_db.commit()
    assert wake is not None and wake.status == "armed"

    status, applied = _commit_stop_terminal(
        "steve",
        "main-1",
        identity.turn_id,
        db=main_db,
    )

    assert status == "cancelled"
    assert applied is not None
    row = main_db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='main-1'",
    ).fetchone()
    assert row["status"] == "cancelled"
    state._active_turns.clear()
