"""Retiring a node drops its dependency edges instead of refusing the retirement.

Archive used to refuse outright when any edge touched the node, in either
direction, and name ``remove_dependency`` as the fix. That guard existed because
``node_waits.unmet_prerequisites`` does not filter archived prerequisites, so a silent
detach would block the dependent forever behind a node no projection shows.

Refusing is the wrong half of that trade. The dependent is stranded either way:
the owner must now walk every edge by hand before it can retire anything, which
is exactly the per-node context cost this release exists to remove, and the
dead end issue #1827 reported. Dropping the edges in the same write removes the
stranding risk the guard was protecting against, so the guard has no remaining
job.

What a failed or closed prerequisite means for a *live* dependent is a separate
question and deliberately unchanged here: ``unmet_prerequisites`` still waits on one.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, dependencies, service
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


def _task(db, plan_id: str, title: str):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db,
    )
    db.commit()
    return node


def _row(db, node_id: str):
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _edges(db, node_id: str) -> list[tuple[str, str]]:
    return [
        (str(row["node_id"]), str(row["depends_on_node_id"]))
        for row in db.execute(
            "SELECT * FROM plan_node_dependencies "
            "WHERE node_id=? OR depends_on_node_id=? "
            "ORDER BY created_at,node_id,depends_on_node_id",
            (node_id, node_id),
        )
    ]


def _events(db, node_id: str, event: str) -> list[dict]:
    import json

    return [
        json.loads(row["payload_json"] or "{}")
        for row in db.execute(
            "SELECT * FROM plan_node_events WHERE node_id=? AND event=? "
            "ORDER BY created_at,rowid",
            (node_id, event),
        )
    ]


def _depend(db, plan_id, node_id, on_node_id):
    return mutate(
        plan_id, "add_dependency",
        {
            "node_id": node_id, "depends_on_node_id": on_node_id,
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


def _archive(db, plan_id, node_id, **params):
    return mutate(
        plan_id, "archive_node",
        {
            "node_id": node_id,
            "expected_revision": _row(db, node_id)["revision"],
            **params,
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


def _close(db, plan_id, node_id, reason="superseded"):
    return mutate(
        plan_id, "close_node",
        {
            "node_id": node_id, "reason": reason,
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


# --- the edges go, the retirement does not fail ------------------------------


def test_archiving_drops_every_edge_touching_the_node(db):
    plan = _plan(db)
    upstream = _task(db, plan.id, "Upstream")
    retired = _task(db, plan.id, "Retired")
    downstream = _task(db, plan.id, "Downstream")
    _depend(db, plan.id, retired.id, upstream.id)
    _depend(db, plan.id, downstream.id, retired.id)

    _archive(db, plan.id, retired.id)

    assert _edges(db, retired.id) == []
    assert _row(db, retired.id)["archived_at"] is not None


def test_each_dropped_edge_is_recorded_on_its_dependent(db):
    plan = _plan(db)
    upstream = _task(db, plan.id, "Upstream")
    retired = _task(db, plan.id, "Retired")
    downstream = _task(db, plan.id, "Downstream")
    _depend(db, plan.id, retired.id, upstream.id)
    _depend(db, plan.id, downstream.id, retired.id)

    _archive(db, plan.id, retired.id)

    # The edge the retired node depended on is recorded against the retired
    # node, and the one pointing at it against its dependent -- the same side
    # remove_dependency records, so history reads identically either way.
    assert _events(db, retired.id, "dependency_removed") == [
        {"depends_on_node_id": upstream.id}
    ]
    assert _events(db, downstream.id, "dependency_removed") == [
        {"depends_on_node_id": retired.id}
    ]


def test_the_dependent_is_unblocked_and_becomes_selectable(db):
    plan = _plan(db)
    retired = _task(db, plan.id, "Retired")
    downstream = _task(db, plan.id, "Downstream")
    _depend(db, plan.id, downstream.id, retired.id)
    assert admission.project(downstream.id, db=db)["condition"] == "blocked"

    _archive(db, plan.id, retired.id)

    assert admission.project(downstream.id, db=db)["condition"] != "blocked"
    assert _events(db, downstream.id, "blocked_cleared")


def test_each_unblocked_dependent_cites_its_own_removal(db):
    """Provenance per dependent: never another node's event as the cause."""
    plan = _plan(db)
    retired = _task(db, plan.id, "Retired")
    first = _task(db, plan.id, "First dependent")
    second = _task(db, plan.id, "Second dependent")
    _depend(db, plan.id, first.id, retired.id)
    _depend(db, plan.id, second.id, retired.id)

    _archive(db, plan.id, retired.id)

    for dependent in (first, second):
        removed, cleared = (
            db.execute(
                "SELECT id,causal_event_id FROM plan_node_events "
                "WHERE node_id=? AND event=? ORDER BY created_at DESC LIMIT 1",
                (dependent.id, event),
            ).fetchone()
            for event in ("dependency_removed", "blocked_cleared")
        )
        assert cleared["causal_event_id"] == removed["id"]


def test_a_dependent_with_another_unmet_prerequisite_stays_blocked(db):
    plan = _plan(db)
    other = _task(db, plan.id, "Other prerequisite")
    retired = _task(db, plan.id, "Retired")
    downstream = _task(db, plan.id, "Downstream")
    _depend(db, plan.id, downstream.id, retired.id)
    _depend(db, plan.id, downstream.id, other.id)

    _archive(db, plan.id, retired.id)

    assert admission.project(downstream.id, db=db)["condition"] == "blocked"
    assert _events(db, downstream.id, "blocked_cleared") == []
    assert _edges(db, other.id) == [(downstream.id, other.id)]


def test_closing_drops_the_edges_the_same_way(db):
    plan = _plan(db)
    retired = _task(db, plan.id, "Retired")
    downstream = _task(db, plan.id, "Downstream")
    _depend(db, plan.id, downstream.id, retired.id)

    _close(db, plan.id, retired.id)

    assert _edges(db, retired.id) == []
    assert admission.project(downstream.id, db=db)["condition"] != "blocked"


def test_no_refusal_mentions_removing_dependencies_first(db):
    """The guard is gone, not merely bypassed by the new writer."""
    import plans.archival as archival

    source = " ".join(dir(archival))
    assert "_EDGE_REFUSED" not in source
    assert "_require_no_dependency_edge" not in source


def test_retiring_a_node_with_no_edges_records_nothing(db):
    plan = _plan(db)
    lonely = _task(db, plan.id, "Lonely")

    _archive(db, plan.id, lonely.id)

    assert _events(db, lonely.id, "dependency_removed") == []


# --- remove_dependency reports whether there was an edge ---------------------


def test_remove_dependency_reports_the_edge_it_removed(db):
    plan = _plan(db)
    first = _task(db, plan.id, "First")
    second = _task(db, plan.id, "Second")
    _depend(db, plan.id, second.id, first.id)

    assert dependencies.remove_dependency(
        second.id, first.id, provenance="test", db=db,
    ) is True


def test_remove_dependency_reports_an_absent_edge_rather_than_inventing_one(db):
    plan = _plan(db)
    first = _task(db, plan.id, "First")
    second = _task(db, plan.id, "Second")

    assert dependencies.remove_dependency(
        second.id, first.id, provenance="test", db=db,
    ) is False
    assert _events(db, second.id, "dependency_removed") == []


def test_the_boundary_carries_removed_so_a_retry_can_tell(db):
    plan = _plan(db)
    first = _task(db, plan.id, "First")
    second = _task(db, plan.id, "Second")
    _depend(db, plan.id, second.id, first.id)

    def _remove():
        return mutate(
            plan.id, "remove_dependency",
            {
                "node_id": second.id, "depends_on_node_id": first.id,
                "expected_revision": _row(db, second.id)["revision"],
            },
            actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
        )

    assert _remove().result["removed"] is True
    assert _remove().result["removed"] is False
