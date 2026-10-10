"""A closed task stays on its plan, folded, with the reason it closed (#1887).

Owner decision, 2026-09-24 (c2c04c37): closing used to archive in the same
write, so a task an agent closed vanished from the pane with no trace and its
accountable person never learned it was dropped. A closed task is now settled
work like any other: it stays on the plan, folds, and its row says why. It is
still never live work, and only an explicit archive takes it off the plan.

A closed task is out of the work the way an archived one always was: it is
nobody's child for leaves, containers, selection, acceptance or progress. So
closing a parent's last open child hands the parent back as open work, and a
closed task counts in neither ``done`` nor ``total``.
"""

from __future__ import annotations

import pathlib
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import (
    archival, closure, decision_authz, delegation_child_lifecycle, node_states,
    operator_projection, projections, service, status,
)
from plans.boundary import PlanActor, mutate

HUMAN = PlanActor(None, "user", None, kind="user")
OWNER = PlanActor("owner", "user", "project")
REASON = "superseded by the new approach"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('owner','user','project','Plan owner','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _task(db, plan_id: str, title: str, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", db=db, **kwargs,
    )


def _close(db, node_id: str, reason: str = REASON):
    return closure.close_node(node_id, reason=reason, provenance="test", db=db)


def _pane(db) -> dict[str, dict]:
    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return {node["id"]: node for node in entry["nodes"]}


def test_the_pane_row_says_why_the_task_was_closed(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    kept = _task(db, plan.id, "Kept")
    dropped = _task(db, plan.id, "Dropped")

    _close(db, dropped.id)

    nodes = _pane(db)
    assert nodes[dropped.id]["state"] == "closed"
    assert nodes[dropped.id]["display"] == {"label_key": "finished", "tone_key": "closed"}
    assert nodes[dropped.id]["status_reason"] == REASON
    assert nodes[kept.id]["status_reason"] == "Not started"


def test_the_agent_read_says_why_the_task_was_closed(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    _task(db, plan.id, "Kept")
    dropped = _task(db, plan.id, "Dropped")

    _close(db, dropped.id)

    read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)
    row = next(row for row in read["recent"]["rows"] if row["id"] == dropped.id)
    assert row["reason"] == REASON


def test_a_closed_task_folds_and_is_never_live_work(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    kept = _task(db, plan.id, "Kept")
    dropped = _task(db, plan.id, "Dropped")
    service.transition_node(dropped.id, "active", db=db)

    _close(db, dropped.id)

    rows = [dict(row) for row in service._plan_tree_rows(db, plan.id)]
    assert dropped.id in node_states.folded_ids(rows)
    assert projections.liveness_signal("owner", db=db).node_ids == (kept.id,)
    assert projections.progress(plan.id, db=db).open == 1


def test_a_parent_closes_over_children_that_are_already_settled(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    step = _task(db, plan.id, "Open the PR", parent_id=phase.id)
    service.transition_node(phase.id, "active", db=db)
    _close(db, step.id)

    _close(db, phase.id)

    assert service._node_row(db, phase.id)["state"] == "closed"
    assert service._node_row(db, step.id)["archived_at"] is None


def test_a_parent_cannot_close_over_an_open_child(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    step = _task(db, plan.id, "Open the PR", parent_id=phase.id)

    with pytest.raises(service.PlanError, match=step.id):
        _close(db, phase.id)

    assert service._node_row(db, phase.id)["state"] == "pending"


def test_close_reasons_names_only_closed_nodes(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    kept = _task(db, plan.id, "Kept")
    dropped = _task(db, plan.id, "Dropped")
    _close(db, dropped.id)

    rows = [service._node_row(db, kept.id), service._node_row(db, dropped.id)]
    assert closure.close_reasons(db, rows) == {dropped.id: REASON}
    assert closure.close_reasons(db, rows[:1]) == {}


def _phase(db, plan_id: str):
    return service.add_node(
        plan_id, kind="phase", title="Ship the feature",
        acceptance_criteria="Shipped", db=db,
    )


def _parent_with_child(db, plan_id: str):
    parent = _phase(db, plan_id)
    child = _task(db, plan_id, "Open the PR", parent_id=parent.id)
    service.transition_node(parent.id, "active", db=db)
    return parent, child


def test_closing_the_last_open_child_hands_the_parent_back_as_work(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent, child = _parent_with_child(db, plan.id)

    _close(db, child.id)

    assert projections.open_delegated_node_ids(db, plan.id, limit=5) == (parent.id,)
    assert projections.movable_work(db, plan.id)
    assert projections.liveness_signal("owner", db=db).node_ids == (parent.id,)
    rows = [dict(row) for row in service._plan_tree_rows(db, plan.id)]
    assert parent.id not in node_states.folded_ids(rows)


def test_an_unstarted_parent_over_only_closed_children_is_open_work(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id)
    child = _task(db, plan.id, "Open the PR", parent_id=parent.id)

    _close(db, child.id)

    rows = [dict(row) for row in service._plan_tree_rows(db, plan.id)]
    assert parent.id not in node_states.folded_ids(rows)
    assert [row["id"] for row in node_states.leaves(rows)] == [parent.id]


def test_a_closed_task_counts_in_neither_done_nor_total(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Ship", acceptance_criteria="Shipped", db=db,
    )
    _task(db, plan.id, "First", parent_id=phase.id)
    _task(db, plan.id, "Second", parent_id=phase.id)
    dropped = _task(db, plan.id, "Dropped", parent_id=phase.id)

    _close(db, dropped.id)

    progress = projections.progress(plan.id, db=db)
    assert (progress.done, progress.total, progress.open) == (0, 2, 2)


def test_a_parent_with_a_closed_child_can_still_be_accepted(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent, child = _parent_with_child(db, plan.id)
    _close(db, child.id)

    for operation, params in (
        ("request_review", {"evidence": "Shipped without the PR step"}),
        ("accept", {"verification_result": "passed"}),
    ):
        mutate(
            plan.id, operation,
            {
                "node_id": parent.id, **params,
                "expected_revision": service._node_row(db, parent.id)["revision"],
            },
            actor=OWNER, provenance="test", mutation_id=uuid.uuid4().hex, db=db,
        )

    assert service._node_row(db, parent.id)["state"] == "accepted"


def test_a_gone_worker_leaves_no_open_parent_behind(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent, child = _parent_with_child(db, plan.id)

    delegation_child_lifecycle.end_open_work(
        db, "owner", reason="worker gone", provenance="test",
    )

    for node in (parent, child):
        row = service._node_row(db, node.id)
        assert (row["state"], row["archived_at"]) == ("closed", None)


# Every reader that splits a plan into parents and leaves asks
# node_states.parent_ids, so a parent handed back by a closed child reads as
# a leaf in all of them at once: the pane, the plan counts and the agent read.


def _phase_over_closed_child(db, *, started: bool):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    child = _task(db, plan.id, "Open the PR", parent_id=phase.id)
    if started:
        service.transition_node(phase.id, "active", db=db)
    _close(db, child.id)
    return plan, phase, child


def test_a_started_parent_over_closed_children_is_a_leaf_in_every_read(db):
    plan, phase, child = _phase_over_closed_child(db, started=True)

    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    nodes = {node["id"]: node for node in entry["nodes"]}
    assert nodes[phase.id]["branch_progress"] is None
    distribution = entry["progress"]["distribution"]
    assert "closed" not in distribution
    assert sum(distribution.values()) == 1
    read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)
    assert [row["id"] for row in read["current"]["rows"]] == [phase.id]
    assert child.id in [row["id"] for row in read["recent"]["rows"]]


def test_an_unstarted_parent_over_closed_children_reads_as_open_not_closed(db):
    plan, phase, _child = _phase_over_closed_child(db, started=False)

    node = _pane(db)[phase.id]
    assert node["display"] != {"label_key": "finished", "tone_key": "closed"}
    assert node["branch_progress"] is None
    read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)
    assert [row["id"] for row in read["current"]["rows"]] == [phase.id]


def test_a_closed_child_leaves_its_branch_count(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    _task(db, plan.id, "First", parent_id=phase.id)
    dropped = _task(db, plan.id, "Dropped", parent_id=phase.id)

    _close(db, dropped.id)

    assert _pane(db)[phase.id]["branch_progress"] == {"done": 0, "total": 1}


# A closed branch is out of the work on every surface, as an archived branch
# always was: closing a node ends the mutable life of its whole subtree
# (node_states.work_ids, service.require_live_node).


def _settle(db, node_id: str, state: str) -> None:
    service.transition_node(node_id, "active", db=db)
    if state == "failed":
        service.transition_node(node_id, "failed", db=db)
        return
    plan_id = service._node_row(db, node_id)["plan_id"]
    for operation, params in (
        ("request_review", {"evidence": "Proven"}),
        ("accept", {"verification_result": "passed"}),
    ):
        mutate(
            plan_id, operation,
            {
                "node_id": node_id, **params,
                "expected_revision": service._node_row(db, node_id)["revision"],
            },
            actor=OWNER, provenance="test", mutation_id=uuid.uuid4().hex, db=db,
        )


def _closed_branch(db):
    """A live phase over an open step and a closed phase whose children settled."""
    plan = service.ensure_plan("thread", "owner", db=db)
    live = _phase(db, plan.id)
    service.transition_node(live.id, "active", db=db)
    open_step = _task(db, plan.id, "Open step", parent_id=live.id)
    closed = service.add_node(
        plan.id, kind="phase", title="Dropped approach",
        acceptance_criteria="Dropped", parent_id=live.id, db=db,
    )
    passed = _task(db, plan.id, "Passed step", parent_id=closed.id)
    failed = _task(db, plan.id, "Failed step", parent_id=closed.id)
    _settle(db, passed.id, "accepted")
    _settle(db, failed.id, "failed")
    _close(db, closed.id)
    return plan, live, open_step, closed, (passed.id, failed.id)


def test_a_closed_branch_is_out_of_the_work_in_every_read(db):
    plan, live, open_step, _closed, beneath = _closed_branch(db)

    rows = [dict(row) for row in service._plan_tree_rows(db, plan.id)]
    assert [row["id"] for row in node_states.leaves(rows)] == [open_step.id]
    assert projections.liveness_signal("owner", db=db).node_ids == (open_step.id,)
    progress = projections.progress(plan.id, db=db)
    assert (progress.done, progress.total) == (0, 1)
    assert _pane(db)[live.id]["branch_progress"] == {"done": 0, "total": 1}
    read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)
    assert not set(beneath) & {row["id"] for row in read["current"]["rows"]}


def test_nothing_is_added_to_or_bound_to_a_closed_node(db):
    plan, _live, open_step, closed, _beneath = _closed_branch(db)

    with pytest.raises(service.PlanError, match="node parent is closed"):
        _task(db, plan.id, "Late step", parent_id=closed.id)
    with pytest.raises(service.PlanError, match="live nodes"):
        service.add_dependency(open_step.id, closed.id, db=db)
    with pytest.raises(service.PlanError, match="plan node is closed"):
        service.transition_node(closed.id, "active", db=db)
    for node_id in (closed.id, *_beneath):
        with pytest.raises(PermissionError):
            decision_authz.source_node(db, plan.id, node_id)


def test_a_closed_node_can_still_be_archived(db):
    _plan, _live, _open_step, closed, beneath = _closed_branch(db)

    result = archival.archive_nodes([*beneath, closed.id], provenance="test", db=db)

    assert result["refused"] == []
    assert service._node_row(db, closed.id)["archived_at"] is not None


def test_a_failure_under_a_closed_branch_never_blocks_acceptance(db):
    plan, live, open_step, _closed, _beneath = _closed_branch(db)
    _settle(db, open_step.id, "accepted")

    for operation, params in (
        ("request_review", {"evidence": "Shipped the other way"}),
        ("accept", {"verification_result": "passed"}),
    ):
        mutate(
            plan.id, operation,
            {
                "node_id": live.id, **params,
                "expected_revision": service._node_row(db, live.id)["revision"],
            },
            actor=OWNER, provenance="test", mutation_id=uuid.uuid4().hex, db=db,
        )

    assert service._node_row(db, live.id)["state"] == "accepted"


def test_a_close_names_open_work_anywhere_beneath_it(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    top = _phase(db, plan.id)
    middle = service.add_node(
        plan.id, kind="phase", title="Middle", acceptance_criteria="Done",
        parent_id=top.id, db=db,
    )
    deep = _task(db, plan.id, "Deep step", parent_id=middle.id)
    # The middle used to be failed over its open step; failing over open work is
    # refused now (#731), so the middle stays a container and the open step sits two levels down.

    with pytest.raises(service.PlanError, match=deep.id):
        _close(db, top.id)


def test_raw_archived_filters_do_not_grow():
    """New plan reads ask node_states, not ``archived_at`` alone (#1887).

    Close no longer archives, so a bare ``archived_at IS NULL`` lets closed
    work and everything under it through. Use ``node_states.in_work_sql`` with
    ``outside_retired_branch_sql`` for flat reads, or ``work_ids`` in Python;
    lower the ceiling when a site moves.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "plans"
    count = sum(
        path.read_text().count("archived_at IS NULL") for path in root.glob("*.py")
    )
    assert count <= 33, count
