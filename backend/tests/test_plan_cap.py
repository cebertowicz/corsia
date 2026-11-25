"""Past the cap, the lowest unstarted top-level work moves to Later (``plans.plan_cap``)."""

from __future__ import annotations

from plans import plan_cap, service
from plans.plan_cap import CAP
from tests.plan_close_next_helpers import (
    owner_plan, plan_update, queue_active, revision, row, select, task,
)
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _later(db, node_id: str) -> bool:
    return bool(row(db, node_id)["later"])


def _fill(db, plan_id: str, count: int) -> list[str]:
    return [task(db, plan_id, f"Task {n}").id for n in range(count)]


def test_an_add_past_the_cap_goes_to_later_and_says_so(db):
    plan = owner_plan(db)
    filled = _fill(db, plan.id, CAP)

    result = plan_update(db, plan.id, "add", {
        "title": "One more", "acceptance_criteria": "done", "size": "S",
    })
    added = result["node"]["id"]

    assert _later(db, added)
    assert not any(_later(db, node_id) for node_id in filled)
    assert "It went to Later" in result["landing"]
    assert "later=false" in result["landing"]


def test_an_insert_above_moves_the_lowest_unstarted_item(db):
    plan = owner_plan(db)
    filled = _fill(db, plan.id, CAP)

    result = plan_update(db, plan.id, "insert", {
        "title": "Urgent", "acceptance_criteria": "done", "size": "S", "before_node_id": filled[0],
    })

    assert not _later(db, result["node"]["id"])
    assert _later(db, filled[-1])
    assert not any(_later(db, node_id) for node_id in filled[:-1])
    assert "this moved to Later: 'Task" in result["landing"]
    events = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='node_later'", (filled[-1],),
    ).fetchall()
    assert plan_cap.REASON in events[-1]["payload_json"]


def test_started_work_neither_counts_nor_moves(db):
    plan = owner_plan(db)
    running = task(db, plan.id, "Running").id
    select(db, plan.id, running)
    waiting = task(db, plan.id, "Started, waiting").id
    queue_active(db, waiting)
    persons = task(db, plan.id, "The person's step", assignee="user").id
    filled = _fill(db, plan.id, CAP)

    plan_update(db, plan.id, "add", {"title": "Last", "acceptance_criteria": "done", "size": "S"})

    for node_id in (running, waiting, persons, *filled):
        assert not _later(db, node_id), row(db, node_id)["title"]


def test_a_phase_counts_once_and_moves_whole(db):
    plan = owner_plan(db)
    phase = service.add_node(plan.id, kind="phase", title="Run", acceptance_criteria="done", db=db)
    steps = [
        service.add_node(plan.id, kind="task", title=f"Step {n}", acceptance_criteria="done",
                         parent_id=phase.id, db=db).id
        for n in range(CAP + 2)
    ]
    db.commit()
    _fill(db, plan.id, CAP - 1)

    assert len(plan_cap.unstarted_above(db, plan.id)) == CAP
    task_ids = [r["id"] for r in db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND parent_id IS NULL AND kind='task'", (plan.id,),
    )]
    plan_update(db, plan.id, "insert", {
        "title": "Above all", "acceptance_criteria": "done", "size": "S", "before_node_id": phase.id,
    })

    assert not any(_later(db, step) for step in steps)
    assert _later(db, task_ids[-1])


def test_a_pull_back_stays_and_the_lowest_other_makes_room(db):
    plan = owner_plan(db)
    filled = _fill(db, plan.id, CAP)
    added = plan_update(db, plan.id, "add", {
        "title": "Pulled", "acceptance_criteria": "done", "size": "S",
    })["node"]["id"]
    assert _later(db, added)

    result = plan_update(db, plan.id, "edit", {
        "node_id": added, "expected_revision": revision(db, added), "later": False,
    })

    assert not _later(db, added)
    assert _later(db, filled[-1])
    # The agent is told what made room, in the same words an add's landing uses.
    assert result["landing"] == f"To keep {CAP} items waiting to start, this moved to Later: 'Task {CAP - 1}'."


def test_a_pull_back_with_room_moves_nothing_and_says_nothing(db):
    plan = owner_plan(db)
    pulled = task(db, plan.id, "Pulled").id
    plan_update(db, plan.id, "edit", {"node_id": pulled, "expected_revision": revision(db, pulled), "later": True})

    result = plan_update(db, plan.id, "edit", {
        "node_id": pulled, "expected_revision": revision(db, pulled), "later": False,
    })

    assert not _later(db, pulled)
    assert "landing" not in result


def test_the_first_spill_runs_once_over_every_agent_plan(db):
    plan = owner_plan(db)
    filled = _fill(db, plan.id, CAP + 3)

    counts = plan_cap.spill_all(db)

    assert counts == {"owner": 3}
    assert [_later(db, node_id) for node_id in filled] == [False] * CAP + [True] * 3
    assert plan_cap.spill_all(db) == {}


def test_a_stranded_task_counts_for_no_item_and_stops_no_add(db):
    """A live task the order reader appends after the tree (its parent archived out from
    under it) belongs to no top-level item: the cap leaves it alone, and adds and the
    first spill still run."""
    plan = owner_plan(db)
    phase = service.add_node(plan.id, kind="phase", title="Gone", acceptance_criteria="done", db=db)
    stranded = service.add_node(
        plan.id, kind="task", title="Stranded", acceptance_criteria="done", parent_id=phase.id, db=db,
    ).id
    # No operation archives a phase over open work, so the shape is seeded.
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at=? WHERE id=?", ("2026-11-25T00:00:00+00:00", phase.id))
    db.commit()
    filled = _fill(db, plan.id, CAP)

    result = plan_update(db, plan.id, "add", {"title": "One more", "acceptance_criteria": "done", "size": "S"})

    assert _later(db, result["node"]["id"])
    assert not _later(db, stranded)
    assert not any(_later(db, node_id) for node_id in filled)
    assert plan_cap.spill_all(db) == {}


def test_a_bulk_pull_back_names_what_made_room(db):
    plan = owner_plan(db)
    filled = _fill(db, plan.id, CAP)
    added = plan_update(db, plan.id, "add", {
        "title": "Pulled", "acceptance_criteria": "done", "size": "S",
    })["node"]["id"]

    result = plan_update(db, plan.id, "edit", {"node_ids": [added], "later": False})

    assert not _later(db, added)
    assert _later(db, filled[-1])
    # The same line the single pull back gives.
    assert result["landing"] == f"To keep {CAP} items waiting to start, this moved to Later: 'Task {CAP - 1}'."
