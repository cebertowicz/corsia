"""Agents see the top of their plan: plan order is priority, and every add is placed.

Three promises. Every owner's thin plan status (its digest line and checkpoint)
lists the top of its plan in order, one short line each, inside the status
budget. An owner's own top-level add or insert gets that list and a landing line
naming the call that places it; an add by anyone else reaches the owner as one
digest line. A move by anyone but the owner (the user dragging a row in the
pane, a conductor) reaches the owner as one digest line. The expected strings
restate the shipped copy independently of production.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from plans import service, status_context
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import owner_plan, plan_update, queue_active, revision, task
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture

OWNER = PlanActor("owner", "user", None)
WORKSPACE_CONDUCTOR = PlanActor("wk-conductor", "user", None)
ORDER = (
    "Plan order is priority. Each number is order_key: the item's place among "
    "your plan's top-level items, 0 at the top."
)


def _owner_thread(db) -> None:
    """The owner's live thread, so lines meant for it have someone to reach."""
    if db.execute("SELECT 1 FROM threads WHERE id='owner'").fetchone() is None:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('owner','user','test/model','t','t')",
        )
    db.commit()


def _conductor(db) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('wk-conductor','user','test/model','CORSIA','t','t')",
    )
    db.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        ("corsia_agent_thread_id", json.dumps({"session_id": "wk-conductor"}), "t"),
    )
    db.commit()


def _status(db, plan_id: str) -> dict:
    return status_context.projection(db, service._plan_row(db, plan_id), actor=OWNER)


def _add(db, plan_id: str, title: str, *, actor=OWNER, **params) -> dict:
    body = {
        "kind": "task", "title": title, "acceptance_criteria": f"{title} is verified",
        "size": "unknown", **params,
    }
    return plan_update(db, plan_id, "add", body, actor=actor)


def _lines(db, plan_id: str, kind: str) -> list[str]:
    return [
        row["summary"] for row in db.execute(
            "SELECT summary FROM context_change_journal WHERE object_kind='plan' "
            "AND object_id=? AND change_kind=? ORDER BY seq",
            (plan_id, kind),
        )
    ]


def _reorder(db, plan_id: str, node_id: str, order_key: int, *, actor=OWNER) -> dict:
    return plan_update(db, plan_id, "reorder", {
        "node_id": node_id, "expected_revision": revision(db, node_id), "order_key": order_key,
    }, actor=actor)


# --- the status every owner gets ---------------------------------------------


def test_the_status_lists_the_top_of_the_plan_in_order_with_each_place(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "Fix the outage")
    task(db, plan.id, "Someday cleanup", later=True)
    task(db, plan.id, "Write the report")
    queue_active(db, first.id)

    top = _status(db, plan.id)["top_of_plan"]

    assert top["order"] == ORDER
    # Later keeps its place (1) for reorder, but is not listed; the tail counts it.
    assert [line.split(" · ")[0] for line in top["items"]] == ["0 Fix the outage", "2 Write the report"]
    assert top["more"] == "1 in Later"


def test_a_waiting_row_names_what_it_waits_on(db):
    plan = owner_plan(db)
    parked = task(db, plan.id, "Ship the fix")
    queue_active(db, parked.id)
    plan_update(db, plan.id, "arm_watch", {
        "node_id": parked.id, "expected_revision": revision(db, parked.id),
        "condition": {"type": "deadline",
                      "value": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
        "expected_event": "ci.done",
    })

    (line,) = _status(db, plan.id)["top_of_plan"]["items"]

    assert line.startswith("0 Ship the fix · waiting")


def test_a_dated_wait_or_blocker_shows_its_own_words_not_its_date(db):
    plan = owner_plan(db)
    parked, stuck = task(db, plan.id, "Board report"), task(db, plan.id, "Map reprint")
    for node in (parked, stuck):
        queue_active(db, node.id)
    plan_update(db, plan.id, "arm_watch", {
        "node_id": parked.id, "expected_revision": revision(db, parked.id),
        "condition": {"type": "deadline",
                      "value": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()},
        "reason": "treasurer sends Q3 numbers",
    })
    plan_update(db, plan.id, "block_node", {
        "node_id": stuck.id, "expected_revision": revision(db, stuck.id), "owner_kind": "external",
        "reason": "vendor has not replied", "reason_key": "vendor",
    })

    items = _status(db, plan.id)["top_of_plan"]["items"]

    assert items == [
        "0 Board report · waiting: treasurer sends Q3 numbers",
        "1 Map reprint · waiting: vendor has not replied",
    ]


def test_the_status_lists_ten_and_counts_the_rest(db):
    plan = owner_plan(db)
    for index in range(14):
        task(db, plan.id, f"Item {index}")

    top = _status(db, plan.id)["top_of_plan"]

    assert len(top["items"]) == 10
    assert top["items"][9].startswith("9 Item 9")
    assert top["more"] == "4 more open below these"


def test_the_status_stays_inside_its_budget_at_the_ceiling(db):
    plan = owner_plan(db)
    for index in range(30):
        node = task(db, plan.id, f"{index:02d} " + "x" * 57)
        queue_active(db, node.id)

    thin = _status(db, plan.id)

    assert len(status_context.render(thin).encode()) <= status_context.CHECKPOINT_BYTES
    top = thin["top_of_plan"]
    assert 1 <= len(top["items"]) <= 10
    assert top["more"] == f"{30 - len(top['items'])} more open below these"


def test_a_plan_with_nothing_open_lists_no_top(db):
    plan = owner_plan(db)

    assert "top_of_plan" not in _status(db, plan.id)


# --- an add is placed ----------------------------------------------------------


def test_an_owners_add_gets_the_top_and_a_landing_line(db):
    plan = owner_plan(db)
    task(db, plan.id, "Fix the outage")

    result = _add(db, plan.id, "New ask")

    node = result["node"]
    assert result["top_of_plan"]["items"][-1].startswith("1 New ask")
    assert result["landing"] == (
        f"Added at 1, the bottom of your plan. Leave it there, or move it: reorder node_id={node['id']} "
        f"expected_revision={node['revision']} order_key=<the number of the item it goes above>."
    )


def test_an_add_below_the_top_ten_still_shows_its_own_line(db):
    plan = owner_plan(db)
    for index in range(12):
        task(db, plan.id, f"Item {index}")

    result = _add(db, plan.id, "New ask")

    items = result["top_of_plan"]["items"]
    assert len(items) == 11 and items[-1].startswith("12 New ask")
    assert result["top_of_plan"]["more"] == "2 more open below these"


def test_an_insert_lands_where_it_was_put(db):
    plan = owner_plan(db)
    task(db, plan.id, "First")
    second = task(db, plan.id, "Second")

    result = plan_update(db, plan.id, "insert", {
        "kind": "task", "title": "Inserted", "acceptance_criteria": "Inserted is verified",
        "size": "unknown", "before_node_id": second.id,
    })

    assert result["landing"].startswith("Added at 1. Leave it there, or move it:")


def test_a_nested_add_gets_no_placing_reply(db):
    plan = owner_plan(db)
    phase = service.add_node(plan.id, kind="phase", title="A phase", acceptance_criteria="Done", db=db)
    db.commit()

    result = _add(db, plan.id, "Child step", parent_id=phase.id)

    assert "top_of_plan" not in result and "landing" not in result


def test_a_conductors_add_tells_the_owner_and_not_the_conductor(db):
    _owner_thread(db)
    _conductor(db)
    plan = owner_plan(db)
    task(db, plan.id, "Ready work")

    result = _add(db, plan.id, "From the conductor", actor=WORKSPACE_CONDUCTOR)

    assert "top_of_plan" not in result
    node = result["node"]
    assert _lines(db, plan.id, "plan.added_by_other") == [
        f"CORSIA (wk-conductor) added 'From the conductor' ({node['id']}) to your plan, at 1. "
        f"Place it against the rest of your plan: leave it there, or reorder "
        f"node_id={node['id']} expected_revision={node['revision']} "
        "order_key=<the number of the item it goes above>."
    ]


def test_an_owners_own_add_leaves_no_digest_line(db):
    _owner_thread(db)
    plan = owner_plan(db)

    _add(db, plan.id, "Mine")

    assert _lines(db, plan.id, "plan.added_by_other") == []


# --- a move by someone else ------------------------------------------------------


def test_a_conductors_move_tells_the_owner(db):
    _owner_thread(db)
    _conductor(db)
    plan = owner_plan(db)
    task(db, plan.id, "First")
    moved = task(db, plan.id, "Second")

    _reorder(db, plan.id, moved.id, 0, actor=WORKSPACE_CONDUCTOR)

    assert _lines(db, plan.id, "plan.moved_by_other") == [
        f"CORSIA (wk-conductor) moved 'Second' ({moved.id}) from 1 to 0 in your plan. "
        "Plan order is priority: work it in its new place."
    ]


def test_the_owners_own_move_and_a_move_to_the_same_place_tell_nobody(db):
    _owner_thread(db)
    _conductor(db)
    plan = owner_plan(db)
    first = task(db, plan.id, "First")
    task(db, plan.id, "Second")

    _reorder(db, plan.id, first.id, 1)
    _reorder(db, plan.id, first.id, 1, actor=WORKSPACE_CONDUCTOR)

    assert _lines(db, plan.id, "plan.moved_by_other") == []
