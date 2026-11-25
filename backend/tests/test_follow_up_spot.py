"""A follow-up keeps its source's place in plan order (``plans.followups._source_spot``).

Plan order is priority: a redo after a send-back, or what an accepted step left
over, stays where its source was instead of dropping to the bottom, and so is
not the first item the cap moves to Later.
"""

from __future__ import annotations

from plans import node_order
from plans.plan_cap import CAP
from tests.plan_close_next_helpers import (
    close, owner_plan, plan_update, request_review, revision, row, select, task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _open_top(db, plan_id: str) -> list[str]:
    return [
        str(node["id"]) for node, _path in node_order.open_nodes_with_paths(db, plan_id)
        if node["parent_id"] is None
    ]


def _reviewed(db, plan_id: str, titles: list[str], at: int):
    """Open tasks with these titles; the one at ``at`` is started and up for review."""
    # Items above the source are the person's steps, so the start-order check lets it start.
    ids = [
        task(db, plan_id, title, **({"assignee": "user"} if n < at else {})).id
        for n, title in enumerate(titles)
    ]
    select(db, plan_id, ids[at])
    request_review(db, plan_id, ids[at])
    return ids


def test_a_redo_after_a_send_back_takes_its_sources_place(db):
    plan = owner_plan(db)
    ids = _reviewed(db, plan.id, ["A", "B", "C", "D"], at=1)

    plan_update(db, plan.id, "reject", {
        "node_id": ids[1], "feedback": "Needs another pass", "expected_revision": revision(db, ids[1]),
    })

    redo = next(str(r["id"]) for r in db.execute("SELECT id FROM plan_nodes WHERE follow_up_of=?", (ids[1],)))
    assert _open_top(db, plan.id) == [ids[0], redo, ids[2], ids[3]]


def test_an_accepted_follow_up_takes_its_sources_place(db):
    plan = owner_plan(db)
    ids = _reviewed(db, plan.id, ["A", "B", "C", "D"], at=1)

    result = close(db, plan.id, "accept_with_follow_up", ids[1])

    assert _open_top(db, plan.id) == [ids[0], result["follow_up"]["id"], ids[2], ids[3]]


def test_a_sidetrack_follow_up_sits_right_after_its_open_source(db):
    plan = owner_plan(db)
    ids = [task(db, plan.id, title).id for title in ["A", "B", "C"]]

    result = plan_update(db, plan.id, "add_follow_up", {
        "node_id": ids[0], "expected_revision": revision(db, ids[0]),
        "title": "Side", "acceptance_criteria": "done", "size": "S",
    })

    follow_up = next(
        str(r["id"]) for r in db.execute("SELECT id FROM plan_nodes WHERE follow_up_of=?", (ids[0],))
    )
    assert result is not None
    assert _open_top(db, plan.id) == [ids[0], follow_up, ids[1], ids[2]]


def test_past_the_cap_the_next_add_moves_the_lowest_item_not_the_redo(db):
    plan = owner_plan(db)
    ids = _reviewed(db, plan.id, [f"Task {n}" for n in range(CAP)], at=0)
    plan_update(db, plan.id, "reject", {
        "node_id": ids[0], "feedback": "Needs another pass", "expected_revision": revision(db, ids[0]),
    })
    redo = next(str(r["id"]) for r in db.execute("SELECT id FROM plan_nodes WHERE follow_up_of=?", (ids[0],)))

    plan_update(db, plan.id, "add", {"title": "One more", "acceptance_criteria": "done", "size": "S"})

    assert not row(db, redo)["later"]
    assert _open_top(db, plan.id)[0] == redo
