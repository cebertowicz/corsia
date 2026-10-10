"""The triage standing reads the open work, never the plan's settled history.

``triage_lines.standing`` runs inside the plan write for ``add`` and a named
start. A plan keeps every settled node, so a read that grows with them grows
for the life of the plan: the standing must return the same answer as a read of
the whole plan while touching only the open nodes.
"""

from __future__ import annotations

import plans.node_order as node_order
import plans.node_states as node_states
import plans.selection_validity as validity
import plans.triage_lines as triage_lines
from plans import closure, service
from tests.plan_close_next_helpers import owner_plan, task
from tests.plan_execution_helpers import add_task
from tests.plan_review_helpers import submit_review_evidence
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _phase(db, plan_id: str, title: str, parent_id: str | None = None):
    node = service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} is verified",
        parent_id=parent_id, db=db,
    )
    db.commit()
    return node


def _child(db, plan_id: str, parent_id: str, title: str):
    node = add_task(db, plan_id, title, parent_id=parent_id)
    db.commit()
    return node


def _settle(db, node_id: str, final: str = "accepted") -> None:
    service.transition_node(node_id, "active", db=db)
    if final == "failed":
        service.transition_node(node_id, "failed", db=db)
    else:
        submit_review_evidence(node_id, db=db)
        service.transition_node(
            node_id, "accepted", verification_result="passed", verified_by="owner", db=db,
        )
    db.commit()


def _whole_plan_standing(db, plan) -> dict:
    """The answer from reading every node of the plan, restated independently."""
    ordered = node_order.ordered_nodes_with_paths(db, str(plan["id"]))
    rows = [row for row, _path in ordered]
    roots = [row for row in rows if row["parent_id"] is None]
    root_of = {str(row["id"]): path[0] if path else str(row["id"]) for row, path in ordered}
    leaves = [
        row for row in node_states.leaves(rows)
        if row["kind"] == "task" and row["state"] in node_order.OPEN_TASK_STATES
    ]
    blockers = validity.slot_blockers(db, [(row, plan) for row in leaves])
    eligible = [str(row["id"]) for row in leaves if blockers[str(row["id"])] is None]
    return {
        "open_roots": [
            str(row["id"]) for row in roots
            if row["state"] in triage_lines.RANKED_STATES
        ],
        "ready": {root_of[node_id] for node_id in eligible},
        "root_of": root_of,
        "positions": {str(row["id"]): index for index, row in enumerate(roots)},
    }


def _scene(db):
    """Settled children under open parents, closed branches and nested phases."""
    plan = owner_plan(db)
    pid = plan.id
    first = task(db, pid, "Settled task, never reopened")
    _settle(db, first.id)

    mixed = _phase(db, pid, "Phase with settled and open children")
    done = _child(db, pid, mixed.id, "Accepted child")
    _settle(db, done.id)
    failed = _child(db, pid, mixed.id, "Failed child")
    _settle(db, failed.id, "failed")
    _child(db, pid, mixed.id, "Open child")

    only_settled = _phase(db, pid, "Open phase whose children all settled")
    gone = _child(db, pid, only_settled.id, "Accepted only child")
    _settle(db, gone.id)

    closed = _phase(db, pid, "Closed branch")
    closure.close_node(closed.id, reason="No longer wanted", db=db)
    db.commit()

    outer = _phase(db, pid, "Outer phase")
    inner = _phase(db, pid, "Inner phase", parent_id=outer.id)
    deep_done = _child(db, pid, inner.id, "Settled deep task")
    _settle(db, deep_done.id)
    _child(db, pid, inner.id, "Open deep task")
    sibling_phase = _phase(db, pid, "Nested empty phase", parent_id=outer.id)
    _child(db, pid, sibling_phase.id, "Open nested task")

    task(db, pid, "Open top-level task")
    return service._plan_row(db, pid)


def test_standing_matches_the_whole_plan_read(db):
    plan = _scene(db)
    expected = _whole_plan_standing(db, plan)

    where = triage_lines.standing(db, plan)

    assert [str(row["id"]) for row in where.open_roots] == expected["open_roots"]
    assert set(where.ready) == expected["ready"]
    assert where.ready  # the scene has ready work, so the comparison is not vacuous
    for node_id, root in where.root_of.items():
        assert expected["root_of"][node_id] == root
    for root_id in expected["open_roots"]:
        assert triage_lines.position(db, str(plan["id"]), root_id) == expected["positions"][root_id]


def test_standing_reads_the_same_rows_however_much_history_the_plan_holds(db, monkeypatch):
    plan = _scene(db)
    reads: list[int] = []
    real = node_order.fetch_all

    def counting(connection, sql, params=()):
        rows = real(connection, sql, params)
        reads.append(len(rows))
        return rows

    def refuse(*_args, **_kwargs):
        raise AssertionError("standing must not read the whole plan in order")

    monkeypatch.setattr(node_order, "fetch_all", counting)
    monkeypatch.setattr(node_order, "ordered_nodes_with_paths", refuse)
    before = triage_lines.standing(db, plan)
    rows_before = list(reads)
    reads.clear()

    for index in range(500):
        settled = add_task(db, plan["id"], f"History {index}")
        _settle(db, settled.id, "failed")

    after = triage_lines.standing(db, plan)

    assert reads == rows_before
    assert set(after.ready) == set(before.ready)
    assert len(after.open_roots) == len(before.open_roots)
