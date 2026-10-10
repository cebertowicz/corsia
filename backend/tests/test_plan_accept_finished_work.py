"""Mark done gives agents one explicit call for their own finished work.

Before this, accepting your own finished task took the overloaded ``accept``
operation while the human surface called the same outcome Mark done. Fresh weak
models still chose request_review or close_node instead. Agent ``mark_done`` now
submits evidence and accepts active work through the same canonical writers.
The legacy active-accept shape remains compatible, and review_requested work
still uses ``accept``.
"""

from __future__ import annotations

import pytest

from plans import boundary_dispatch, holds, service
from plans.boundary import mutate
from tests.test_attention_queue import _mid
from tests.test_plan_archival import OWNER, _plan, _row, _task, db  # noqa: F401 - fixture


def _active(db, plan_id: str, title: str = "Ship it"):
    node = _task(db, plan_id, title=title)
    mutate(plan_id, "start", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)
    db.commit()
    assert _row(db, node.id)["state"] == "active"
    return node


def _accept(db, plan_id: str, node, operation: str = "accept", **extra):
    return mutate(plan_id, operation, {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "verification_result": "passed", **extra,
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)


def _mark_done(db, plan_id: str, node, **extra):
    return mutate(plan_id, "mark_done", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        **extra,
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)


def _evidence(db, node_id: str) -> list[str]:
    rows = db.execute(
        "SELECT event FROM plan_node_events WHERE node_id=? "
        "AND event='review_evidence_submitted' ORDER BY rowid",
        (node_id,),
    ).fetchall()
    return [row["event"] for row in rows]


def test_mark_done_settles_active_work_with_evidence_in_one_call(db):
    plan = _plan(db)
    node = _active(db, plan.id)

    _mark_done(db, plan.id, node, evidence="Merged as abc123; 12 tests pass.")

    assert _row(db, node.id)["state"] == "accepted"
    assert _evidence(db, node.id) == ["review_evidence_submitted"]


def test_mark_done_requires_evidence_even_beyond_the_tool_schema(db):
    plan = _plan(db)
    node = _active(db, plan.id)

    with pytest.raises(service.PlanError, match="mark_done with evidence"):
        _mark_done(db, plan.id, node)

    assert _row(db, node.id)["state"] == "active"


@pytest.mark.parametrize(
    ("state", "message"),
    [("review_requested", "use accept")],
)
def test_mark_done_names_the_operation_for_reviewed_work(db, state, message):
    plan = _plan(db)
    node = _task(db, plan.id, title="Ship it")
    if state == "review_requested":
        node = _active(db, plan.id)
        mutate(plan.id, "request_review", {
            "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
            "evidence": "Ready.",
        }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)

    with pytest.raises(service.PlanError, match=message):
        _mark_done(db, plan.id, node, evidence="Done.")

    assert _row(db, node.id)["state"] == state


def _start(db, plan_id: str, node) -> dict:
    return mutate(plan_id, "start", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db).result


def test_a_start_the_slot_refuses_says_it_did_not_start_and_how_to(db):
    """#3273: the collision was only a structured conflict, so it read as done."""
    plan = _plan(db)
    current = _active(db, plan.id, title="Current work")
    other = _task(db, plan.id, title="Already merged")

    reply = _start(db, plan.id, other)

    assert _row(db, other.id)["state"] == "pending"
    assert reply["notice"] == (
        f"Not started: Current work ({current.id}) holds your executor slot. "
        "Settle it, or set it aside with arm_watch or block_node, and pass "
        f"next_node_id='{other.id}' on that call to start this task in the same write."
    )


def test_a_start_that_takes_the_slot_carries_no_notice(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Ship it")

    reply = _start(db, plan.id, node)

    assert _row(db, node.id)["state"] == "active"
    assert "notice" not in reply


def test_mark_done_on_pending_work_names_the_task_holding_the_slot(db):
    """'start it first' pointed at the start the slot had just refused."""
    plan = _plan(db)
    current = _active(db, plan.id, title="Current work")
    other = _task(db, plan.id, title="Already merged")

    with pytest.raises(service.PlanError) as refused:
        _mark_done(db, plan.id, other, evidence="Merged.")

    assert str(refused.value) == (
        f"mark_done requires active work, and Current work ({current.id}) holds "
        "your executor slot, so this task cannot be started yet. Settle that task "
        f"or set it aside with next_node_id='{other.id}', then mark_done this one."
    )
    assert "start it first" not in str(refused.value)


def test_accept_settles_active_work_with_evidence_in_one_call(db):
    plan = _plan(db)
    node = _active(db, plan.id)

    _accept(db, plan.id, node, evidence="Merged as abc123; 12 tests pass.")

    assert _row(db, node.id)["state"] == "accepted"
    assert _evidence(db, node.id) == ["review_evidence_submitted"]


def test_accept_with_follow_up_settles_active_work_too(db):
    plan = _plan(db)
    node = _active(db, plan.id)

    _accept(
        db, plan.id, node, "accept_with_follow_up", evidence="Core shipped.",
        title="Polish", acceptance_criteria="Polish DOD",
    )

    assert _row(db, node.id)["state"] == "accepted_with_followup"
    follow_up = db.execute(
        "SELECT state FROM plan_nodes WHERE follow_up_of=?", (node.id,),
    ).fetchone()
    assert follow_up["state"] == "pending"


def test_accept_on_active_work_without_evidence_names_the_fix(db):
    plan = _plan(db)
    node = _active(db, plan.id)

    with pytest.raises(service.PlanError, match="use mark_done with evidence"):
        _accept(db, plan.id, node)

    assert _row(db, node.id)["state"] == "active"
    assert boundary_dispatch.FINISHED_WORK_NEEDS_EVIDENCE.endswith(
        "work you are not doing."
    )


@pytest.mark.parametrize("operation", ["mark_done", "accept"])
def test_a_stop_on_active_work_still_refuses_one_call_completion(db, operation):
    plan = _plan(db)
    node = _active(db, plan.id)
    holds.pause_hold(
        node.id, "Held", expected_revision=_row(db, node.id)["revision"],
        actor_id="root", provenance="test", db=db,
    )
    db.commit()

    finish = _mark_done if operation == "mark_done" else _accept
    with pytest.raises(service.PlanError):
        finish(db, plan.id, node, evidence="Done anyway.")

    assert _row(db, node.id)["state"] == "active"


def test_accepting_work_already_in_review_is_unchanged(db):
    plan = _plan(db)
    node = _active(db, plan.id)
    mutate(plan.id, "request_review", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "evidence": "Ready.",
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)

    _accept(db, plan.id, node, evidence="Verified.")

    assert _row(db, node.id)["state"] == "accepted"
    # The submission, then the settler's verdict: two records, as before.
    assert len(_evidence(db, node.id)) == 2
