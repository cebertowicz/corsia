"""One canonical plan order: a parent before its children, siblings by order_key, id.

`plans.node_order.ordered_cursor` is the single depth-first walk behind the
current-plan projection, the plan rollup and reconciliation's slot filling, so
the order a plan is shown in is the order its empty executor slots are filled
in. A live row whose parent chain never reaches a root is corrupt hierarchy:
it follows the tree in sibling order with an empty path and a warning names
it, so no reader hides it, skips selecting it, or fails because of it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, current, node_order, rollup, service
from plans.boundary import PlanActor
from tests.plan_writer_authorizer import raw_plan_seed



def _ids_and_paths(ordered) -> list[tuple[str, tuple[str, ...]]]:
    return [(row["id"], path) for row, path in ordered]


def test_depth_first_orders_parents_first_and_siblings_by_order_key_then_id(db):
    plan, expected, _active_tasks = _nested_plan(db)

    ordered = node_order.ordered_nodes_with_paths(db, plan.id)

    assert _ids_and_paths(ordered) == expected


#: Live rows whose parent chain never reaches a root, in the order they follow the tree.
_UNREACHABLE = {
    "archived_or_absent_parent": (("orphan-child", "orphan", 0), ("orphan", "archived-parent", 1)),
    "cycle": (("cycle-x", "cycle-y", 0), ("cycle-y", "cycle-x", 0)),
    "self_parent": (("self-parent", "self-parent", 0),),
}


@pytest.mark.parametrize("shape", sorted(_UNREACHABLE))
def test_depth_first_places_rows_unreachable_from_a_root_after_the_tree(db, shape, caplog):
    """Corrupt hierarchy stays visible after the tree and a warning names it."""
    plan, expected, _active_tasks = _nested_plan(db)
    db.execute("PRAGMA foreign_keys=OFF")
    stranded = []
    for node_id, parent_id, order_key in _UNREACHABLE[shape]:
        node = _node(db, plan.id, "task", node_id, order_key)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (parent_id, node.id))
        stranded.append((node, order_key))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    caplog.set_level(logging.WARNING, logger="plans.node_order")
    caplog.clear()

    ordered = node_order.ordered_nodes_with_paths(db, plan.id)

    expected_stranded = sorted(stranded, key=lambda item: (item[1], item[0].id))
    assert _ids_and_paths(ordered) == [
        *expected,
        *((node.id, ()) for node, _order_key in expected_stranded),
    ]
    [warning] = [record.getMessage() for record in caplog.records]
    assert warning.endswith(", ".join(sorted(node.id for node, _order_key in stranded)))


def test_depth_first_of_no_rows_is_empty(db):
    plan = service.ensure_plan("thread", "owner", db=db)

    assert node_order.ordered_nodes_with_paths(db, plan.id) == []


def test_order_cursor_never_projects_node_bodies(db):
    """The hot ordered projection excludes prose even after recursion completes."""
    sql = node_order._open_cte(db) + node_order._ORDERED_CURSOR_TAIL_SQL

    assert "SELECT * FROM plan_nodes" not in sql
    assert "node.*" not in sql
    assert "description" not in sql
    assert "acceptance_criteria" not in sql


def test_full_ordered_read_looks_up_bodies_by_id_without_scanning_every_plan(db):
    """Bodies join to the plan's ordered rows by key; no read walks all nodes."""
    plan, _expected, _active_tasks = _nested_plan(db)

    steps = [
        row["detail"] for row in db.execute(
            f"EXPLAIN QUERY PLAN {node_order._FULL_ORDERED_SQL}", (plan.id,),
        )
    ]

    assert "SCAN node" not in steps
    assert any(step.startswith("SEARCH node USING") for step in steps)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        """INSERT INTO threads (id,user_id,created_at,updated_at)
           VALUES ('owner','user-1','t','t')""",
    )
    connection.commit()
    yield connection
    connection.close()


def _node(db, plan_id: str, kind: str, title: str, order_key: int, parent_id=None):
    """Append one node, then pin its ``order_key`` exactly (ties included).

    Inserting at an explicit key would shift later siblings, so the key is
    written after the append instead.
    """
    node = service.add_node(
        plan_id, kind=kind, title=title, acceptance_criteria=f"{title} is verified",
        parent_id=parent_id, db=db,
    )
    db.execute("UPDATE plan_nodes SET order_key=? WHERE id=?", (order_key, node.id))
    return node


def _nested_plan(db):
    """Build a real nested plan with interleaved flat order keys and one tie.

    Returns the plan, its expected (node id, ancestor path) order, and the ids
    of its active tasks.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    phase_one = _node(db, plan.id, "phase", "Phase one", 0)
    track = _node(db, plan.id, "track", "Track", 0, phase_one.id)
    a1 = _node(db, plan.id, "task", "a1", 1, track.id)
    a0 = _node(db, plan.id, "task", "a0", 0, track.id)
    a2 = _node(db, plan.id, "task", "a2", 1, phase_one.id)
    phase_two = _node(db, plan.id, "phase", "Phase two", 1)
    b1 = _node(db, plan.id, "task", "b1", 0, phase_two.id)
    loose = _node(db, plan.id, "task", "Loose", 1)
    for task in (a0, a2, b1, loose):
        service.transition_node(task.id, "active", db=db)
    db.commit()
    tail = [(loose.id, ()), (phase_two.id, ()), (b1.id, (phase_two.id,))]
    if phase_two.id < loose.id:
        tail = [tail[1], tail[2], tail[0]]
    expected = [
        (phase_one.id, ()),
        (track.id, (phase_one.id,)),
        (a0.id, (phase_one.id, track.id)),
        (a1.id, (phase_one.id, track.id)),
        (a2.id, (phase_one.id,)),
        *tail,
    ]
    return plan, expected, {a0.id, a2.id, b1.id, loose.id}


def test_current_plan_rollup_and_reconciliation_share_one_order(db):
    plan, expected, active_tasks = _nested_plan(db)
    expected_ids = [node_id for node_id, _path in expected]

    shown = current.read_current_plan(actor=PlanActor("owner", "user-1", None), db=db)
    rolled = rollup.rollup_thread(plan.id, db=db)

    assert [node["id"] for node in shown["nodes"]] == expected_ids
    assert [(node["id"], tuple(node["path"])) for node in rolled.nodes] == expected
    assert [node["depth"] for node in rolled.nodes] == [len(path) for _id, path in expected]
    assert node_order.active_task_ids(db, plan.id) == [
        node_id for node_id in expected_ids if node_id in active_tasks
    ]


def _reconcile(db, plan_id: str) -> None:
    admission.reconcile_plan(plan_id, actor_id=None, provenance="test.node_order", db=db)


def test_eligibility_warns_before_selecting_a_stranded_task(db, caplog):
    """Admission cannot bind corrupt work without naming the hierarchy defect."""
    from plans import selection_validity

    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "phase", "Retired phase", 0)
    stranded = _node(db, plan.id, "task", "Stranded task", 0, phase.id)
    service.transition_node(stranded.id, "active", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (phase.id,))
    db.commit()
    caplog.set_level(logging.WARNING, logger="plans.node_order")
    caplog.clear()

    selected = selection_validity.first_eligible_active_id(
        db, service._plan_row(db, plan.id),
    )

    assert selected == stranded.id
    [warning] = [record.getMessage() for record in caplog.records]
    assert warning.endswith(stranded.id)


def test_every_reader_of_plan_order_keeps_a_task_under_an_archived_phase(db, caplog):
    """No application writer leaves live work under an archived parent.

    Only a foreign write (a repair or migration) can. The current-plan
    projection and the rollup still show the stranded task, reconciliation
    still selects it, and each reader's warning names it.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "phase", "Retired phase", 0)
    stranded = _node(db, plan.id, "task", "Stranded task", 0, phase.id)
    service.transition_node(stranded.id, "active", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (phase.id,))
    db.commit()
    caplog.set_level(logging.WARNING, logger="plans.node_order")

    shown = current.read_current_plan(actor=PlanActor("owner", "user-1", None), db=db)
    rolled = rollup.rollup_thread(plan.id, db=db)
    _reconcile(db, plan.id)

    assert [node["id"] for node in shown["nodes"]] == [stranded.id]
    assert [(node["id"], node["path"]) for node in rolled.nodes] == [(stranded.id, [])]
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (stranded.id,),
    ).fetchone()[0] == 1
    ordered = [record for record in caplog.records if record.name == "plans.node_order"]
    assert len(ordered) >= 3
    assert all(record.getMessage().endswith(stranded.id) for record in ordered)
