"""An archived plan node is immutable, at one central primitive.

`require_live_node`'s docstring already claims that archive ends a node's
mutable life, but at `c314478e` only two of nine writers honoured it: the
boundary guarded the generic mutation path, and every direct service caller —
`decisions`, `decision_lifecycle`, `external_reviews`, the since-deleted markdown importer,
`delegation.harvest_ready_children` — walked
straight past it.

The census below is the whole bypass surface. Each case mutates an archived node
at head and must refuse with the single identity `plan node is archived`.
`service._transition` is the state choke point, so one guard there covers
`transition_node`, `request_review`, `fail_node` and harvest at once; the four
writers that resolve their own row are guarded individually.

Deliberately unguarded: `service.record_runner_event` (append-only diagnostics).
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import delegation, mutations, rejection, service
from tests.test_attention_queue import _seed
from tests.plan_writer_authorizer import raw_plan_seed

_ARCHIVED = "plan node is archived"


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


def _task(db, plan_id: str, title: str = "Stale", **kwargs):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )
    db.commit()
    return node


def _archived_task(db, plan_id: str, title: str = "Stale", *, state="active"):
    """One archived node left in an open state, as every archive writer leaves it."""
    node = _task(db, plan_id, title)
    if state != "pending":
        service.transition_node(node.id, "active", db=db)
    if state == "review_requested":
        service.transition_node(node.id, "review_requested", db=db)
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET archived_at=? WHERE id=?", (service._now(), node.id),
        )
    db.commit()
    return service.get_node(node.id, db=db)


def _row(db, node_id: str):
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _events(db, node_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_events WHERE node_id=?", (node_id,),
    ).fetchone()["n"]


# --- the bypass census: 7 writers, one refusal identity ---------------------


def test_transition_node_refuses_an_archived_node(db):
    """R8. The state choke point, so this one guard also covers R13 and R14."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)
    before = _row(db, node.id)["revision"]

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        service.transition_node(node.id, "review_requested", db=db)
    db.rollback()

    assert _row(db, node.id)["state"] == "active"
    assert _row(db, node.id)["revision"] == before


def test_edit_node_refuses_an_archived_node(db):
    """R9."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        mutations.edit_node(node.id, title="Renamed", db=db)
    db.rollback()

    assert _row(db, node.id)["title"] == "Stale"


def test_split_node_refuses_an_archived_node(db):
    """R10."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)
    before = db.execute("SELECT COUNT(*) AS n FROM plan_nodes").fetchone()["n"]

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        mutations.split_node(
            node.id, title="Remainder", acceptance_criteria="Remainder DOD", db=db,
        )
    db.rollback()

    assert db.execute("SELECT COUNT(*) AS n FROM plan_nodes").fetchone()["n"] == before


def test_reorder_node_refuses_an_archived_node(db):
    """R11."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)
    _task(db, plan.id, "Live sibling")
    before = _row(db, node.id)["order_key"]

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        mutations.reorder_node(node.id, 0, db=db)
    db.rollback()

    assert _row(db, node.id)["order_key"] == before


def test_attach_spawned_thread_refuses_an_archived_node(db):
    """R12. An archived node can never acquire a new delegation."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        delegation.attach_spawned_thread(node.id, "child", db=db)
    db.rollback()

    assert _row(db, node.id)["spawned_thread_id"] is None


def test_fail_node_refuses_an_archived_node(db):
    """R13."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        mutations.fail_node(node.id, db=db)
    db.rollback()

    assert _row(db, node.id)["state"] == "active"


def test_request_review_refuses_an_archived_node(db):
    """R14."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        mutations.request_review(node.id, evidence="Proof", db=db)
    db.rollback()

    assert _row(db, node.id)["state"] == "active"


def test_reject_review_refuses_an_archived_node(db):
    plan = _plan(db)
    node = _archived_task(db, plan.id, state="review_requested")

    with pytest.raises(service.PlanError, match=_ARCHIVED):
        rejection.reject_review(node.id, feedback="Not yet", db=db)
    db.rollback()

    assert _row(db, node.id)["state"] == "review_requested"


# --- what must keep working --------------------------------------------------


def test_runner_events_stay_appendable_on_an_archived_node(db):
    """Diagnostics are append-only and deliberately unguarded."""
    plan = _plan(db)
    node = _archived_task(db, plan.id)
    before = _events(db, node.id)

    service.record_runner_event(
        node.id, "runner_probe", provenance="test", payload={"probe": True}, db=db,
    )
    db.commit()

    assert _events(db, node.id) == before + 1
