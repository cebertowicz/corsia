"""One archive call retires many nodes, and says exactly why each refusal held.

Cleaning a plan used to cost one `archive_node` call per node, each carrying
that node's `expected_revision`, which meant reading the plan first just to
learn the revisions -- the per-node context cost this release exists to cut. A
bulk call takes plain ids: archive deliberately takes no position on a node's
state, so there is no state claim a revision could guard.

The other half is the refusal. `SELECT 1 ... LIMIT 1` could say *that* a node
was blocked but never *which* row blocked it, so an owner cleaning a plan got
"archive is not recursive" and no way to act on it without hunting. Every guard
now captures the blocking rows and names them.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate
from tests.test_attention_queue import _mid, _seed, raise_decision  # noqa: F401

OWNER = PlanActor("root", "user-1", "project-1")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


def _plan(db, owner: str = "root"):
    plan = service.ensure_plan("thread", owner, db=db)
    db.commit()
    return plan


def _task(db, plan_id: str, title: str, kind: str = "task", **kwargs):
    node = service.add_node(
        plan_id, kind=kind, title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )
    db.commit()
    return node


def _phase(db, plan_id: str, title: str, **kwargs):
    """A container node: only a phase or track may hold children."""
    return _task(db, plan_id, title, kind="phase", **kwargs)


def _row(db, node_id: str):
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _archive_many(db, plan_id, node_ids, actor=OWNER, **params):
    return mutate(
        plan_id, "archive_node",
        {"node_ids": list(node_ids), **params},
        actor=actor, provenance="test", mutation_id=_mid(), db=db,
    ).result


def _archive_one(db, plan_id, node_id, **params):
    return mutate(
        plan_id, "archive_node",
        {
            "node_id": node_id,
            "expected_revision": _row(db, node_id)["revision"],
            **params,
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


# --- the bulk body -----------------------------------------------------------


def test_one_call_retires_every_node_it_names(db):
    plan = _plan(db)
    nodes = [_task(db, plan.id, f"Stale {index}") for index in range(5)]
    kept = _task(db, plan.id, "Kept")

    result = _archive_many(db, plan.id, [node.id for node in nodes])

    assert [entry["node_id"] for entry in result["archived"]] == [
        node.id for node in nodes
    ]
    assert result["refused"] == []
    assert all(_row(db, node.id)["archived_at"] is not None for node in nodes)
    assert _row(db, kept.id)["archived_at"] is None


def test_a_bulk_call_records_no_close_next_evidence_it_cannot_have(db, caplog):
    """No next task was named, so the close-next emit must not run and fail."""
    plan = _plan(db)
    node = _task(db, plan.id, "Stale")

    with caplog.at_level("WARNING", logger="plans.boundary"):
        _archive_many(db, plan.id, [node.id])

    assert "close_next emit failed" not in caplog.text


def test_the_bulk_body_needs_no_revision_for_any_node(db):
    """No revision means no read-the-plan-first round trip, which is the point."""
    plan = _plan(db)
    node = _task(db, plan.id, "Stale")

    result = _archive_many(db, plan.id, [node.id])

    assert [entry["node_id"] for entry in result["archived"]] == [node.id]


def test_a_bulk_call_refuses_a_revision_that_cannot_describe_it(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Stale")

    with pytest.raises(service.PlanInputError, match="no single node"):
        _archive_many(db, plan.id, [node.id], expected_revision=1)


def test_the_two_bodies_are_exclusive(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Stale")

    with pytest.raises(service.PlanInputError, match="no single node"):
        _archive_many(db, plan.id, [node.id], node_id=node.id)


def test_naming_no_target_at_all_is_refused(db):
    plan = _plan(db)

    with pytest.raises(service.PlanInputError, match="node_id .*or node_ids"):
        mutate(
            plan.id, "archive_node", {},
            actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
        )


@pytest.mark.parametrize("ids", [[], ["a"] * 201, ["  "], [1]])
def test_an_unusable_node_ids_list_is_refused(db, ids):
    plan = _plan(db)

    with pytest.raises(service.PlanInputError, match="node_ids must be a list"):
        _archive_many(db, plan.id, ids)


def test_a_null_node_ids_is_a_bulk_body_not_an_absent_one(db):
    """The HTTP surface has no schema layer, so presence decides the shape."""
    plan = _plan(db)
    node = _task(db, plan.id, "Stale")

    def _send(params):
        return mutate(
            plan.id, "archive_node", params,
            actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
        )

    with pytest.raises(service.PlanInputError, match="no single node"):
        _send({"node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
               "node_ids": None})
    with pytest.raises(service.PlanInputError, match="node_ids must be a list"):
        _send({"node_ids": None})
    assert _row(db, node.id)["archived_at"] is None


def test_a_node_outside_the_plan_is_refused_for_every_id(db):
    """Authorization is per id: one call must not smuggle a foreign node in."""
    plan = _plan(db)
    other = _plan(db, "other-thread")
    mine = _task(db, plan.id, "Mine")
    theirs = _task(db, other.id, "Theirs")

    with pytest.raises(PermissionError):
        _archive_many(db, plan.id, [mine.id, theirs.id])
    db.rollback()

    assert _row(db, mine.id)["archived_at"] is None


# --- refusals name the exact rows that blocked them --------------------------


def test_a_refusal_names_the_exact_unarchived_children(db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    first = _task(db, plan.id, "First child", parent_id=parent.id)
    second = _task(db, plan.id, "Second child", parent_id=parent.id)

    with pytest.raises(service.PlanError) as refusal:
        _archive_one(db, plan.id, parent.id)

    message = str(refusal.value)
    assert first.id in message and second.id in message


def test_a_bulk_call_reports_each_refusal_without_losing_the_rest(db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    child = _task(db, plan.id, "Child", parent_id=parent.id)
    stale = _task(db, plan.id, "Stale")

    result = _archive_many(db, plan.id, [parent.id, stale.id])

    assert [entry["node_id"] for entry in result["archived"]] == [stale.id]
    assert [entry["node_id"] for entry in result["refused"]] == [parent.id]
    assert child.id in result["refused"][0]["reason"]
    assert _row(db, stale.id)["archived_at"] is not None
    assert _row(db, parent.id)["archived_at"] is None


def test_a_later_refusal_never_undoes_an_earlier_archive(db):
    """Each node is its own savepoint: a refusal rolls back only itself."""
    plan = _plan(db)
    stale = _task(db, plan.id, "Stale")
    parent = _phase(db, plan.id, "Parent")
    _task(db, plan.id, "Child", parent_id=parent.id)

    result = _archive_many(db, plan.id, [stale.id, parent.id])
    db.rollback()  # anything not committed by the call is discarded here

    assert [entry["node_id"] for entry in result["refused"]] == [parent.id]
    assert _row(db, stale.id)["archived_at"] is not None
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='node_archived'",
        (stale.id,),
    ).fetchone()[0] == 1


def test_naming_the_children_first_lets_one_call_clean_the_whole_branch(db):
    """The refusal is actionable, so the obvious retry works."""
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    child = _task(db, plan.id, "Child", parent_id=parent.id)

    result = _archive_many(db, plan.id, [child.id, parent.id])

    assert result["refused"] == []
    assert [entry["node_id"] for entry in result["archived"]] == [
        child.id, parent.id
    ]


def test_an_already_archived_id_is_reported_not_fatal(db):
    """A retried cleanup names nodes the first attempt already retired."""
    plan = _plan(db)
    done = _task(db, plan.id, "Already retired")
    stale = _task(db, plan.id, "Stale")
    _archive_one(db, plan.id, done.id)

    result = _archive_many(db, plan.id, [done.id, stale.id])

    assert [entry["node_id"] for entry in result["archived"]] == [stale.id]
    assert [entry["node_id"] for entry in result["refused"]] == [done.id]


def test_a_lane_owned_node_is_refused_alone_and_names_its_owner(db):
    """An open decision's node is the decision lane's to end, not the cleanup's."""
    decision = raise_decision(db, "root", "root")
    stale = _task(db, decision["plan_id"], "Stale")

    result = _archive_many(db, decision["plan_id"], [decision["node_id"], stale.id])

    assert [entry["node_id"] for entry in result["archived"]] == [stale.id]
    (refused,) = result["refused"]
    assert refused["node_id"] == decision["node_id"]
    assert "decision operations" in refused["reason"]
    assert _row(db, decision["node_id"])["archived_at"] is None


def test_a_long_list_of_blockers_is_bounded_and_never_miscounted(db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    children = [
        _task(db, plan.id, f"Child {index}", parent_id=parent.id)
        for index in range(15)
    ]

    (refused,) = _archive_many(db, plan.id, [parent.id])["refused"]

    named = [child.id for child in children if child.id in refused["reason"]]
    assert len(named) == 10
    assert refused["reason"].endswith(" and more")


def test_a_refusal_never_archives_the_node_it_refused(db):
    plan = _plan(db)
    parent = _phase(db, plan.id, "Parent")
    _task(db, plan.id, "Child", parent_id=parent.id)

    result = _archive_many(db, plan.id, [parent.id])

    assert result["archived"] == []
    assert _row(db, parent.id)["archived_at"] is None


# --- the slot a bulk archive frees -------------------------------------------


def test_bulk_archiving_the_selected_task_frees_its_executors_slot(db):
    """An archived task cannot keep holding the slot the next task needs.

    The single-node path releases the attempt and reconciles the plan; a bulk
    call archiving the same task must leave the executor in the same state, or
    the slot stays occupied by a node that no longer exists to anybody.
    """
    from plans import admission_attempts

    plan = _plan(db)
    working = _task(db, plan.id, "Working")
    waiting = _task(db, plan.id, "Waiting")
    mutate(
        plan.id, "activate",
        {"node_id": working.id, "expected_revision": working.revision},
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )
    held = admission_attempts.slot_holder(db, plan.id, "root")
    assert held is not None and held["node_id"] == working.id

    _archive_many(db, plan.id, [working.id])

    assert admission_attempts.slot_holder(db, plan.id, "root") is None
    attempt = db.execute(
        "SELECT released_at, release_reason FROM plan_node_attempts WHERE node_id=?",
        (working.id,),
    ).fetchone()
    assert attempt["released_at"] is not None
    assert attempt["release_reason"] == "archived"
    # The slot is now free for the task behind it, exactly as a single archive
    # leaves it.
    assert _row(db, waiting.id)["state"] == "pending"
