"""An in-flight dispatch never holds the slot of a task that was parked (#2705).

An already-started tool call on Task A's attempt keeps its dispatch row and
attempt id, so its effect stays attributed to A. It does not keep A selected:
every transition that parks A releases A's attempt at once and selects queued
Task B in the same mutation, even when A's call never settles (a lost
settlement once held the slot for the rest of the Turn). Settling the call
later only closes its row.

The tests reach the real mutation boundary; the settlement test settles the
call through `plans.tool_dispatches.settle`.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import service, tool_dispatches
from tests.test_plan_reconcile_release import (  # noqa: F401 - shared scaffolding
    RELEASES,
    _release_params,
    attempt,
    db,
    open_attempt,
    plan_update,
    queued_pair,
    release_actor,
    revision,
    seed_threads,
)

DISPATCH_ID = "dispatch-in-flight"


def start_dispatch(db, attempt_id: str) -> None:
    """Record one already-started, unsettled tool call on an attempt."""
    db.execute(
        "INSERT INTO plan_tool_dispatches (id,attempt_id,turn_id,tool_name,started_at) "
        "VALUES (?,?,?,?,?)",
        (DISPATCH_ID, attempt_id, "turn-in-flight", "probe_tool", service._now()),
    )
    db.commit()


def dispatch_row(db):
    """Return the in-flight dispatch row."""
    return db.execute(
        "SELECT * FROM plan_tool_dispatches WHERE id=?", (DISPATCH_ID,),
    ).fetchone()


def _archive(db, plan_id: str, node_id: str) -> dict:
    return plan_update(
        db, plan_id, "archive_node",
        {"node_id": node_id, "expected_revision": revision(db, node_id)},
    )


@pytest.mark.parametrize(
    ("operation", "release_reason"), RELEASES, ids=[op for op, _ in RELEASES],
)
def test_parking_a_task_with_an_unsettled_call_frees_its_slot_at_once(
    db, operation, release_reason,
):
    plan, task_a, a_attempt, task_b = queued_pair(db)
    start_dispatch(db, a_attempt)

    plan_update(
        db, plan.id, operation, _release_params(db, plan.id, operation, task_a),
        actor=release_actor(operation),
    )

    assert db.in_transaction is False
    assert attempt(db, a_attempt)["release_reason"] == release_reason
    assert open_attempt(db, task_b)["executor_thread_id"] == "owner"
    row = dispatch_row(db)
    assert (row["attempt_id"], row["settled_at"]) == (a_attempt, None)


def test_settling_the_call_after_the_park_only_closes_its_row(isolated_db):
    connection = get_db()
    try:
        seed_threads(connection)
        plan, task_a, a_attempt, task_b = queued_pair(connection)
        start_dispatch(connection, a_attempt)
        _archive(connection, plan.id, task_a)
        b_attempt = open_attempt(connection, task_b)["id"]

        assert tool_dispatches.settle(DISPATCH_ID, "succeeded") is True

        row = dispatch_row(connection)
        assert (row["attempt_id"], row["outcome"]) == (a_attempt, "succeeded")
        assert attempt(connection, a_attempt)["release_reason"] == "archived"
        assert open_attempt(connection, task_b)["id"] == b_attempt
    finally:
        connection.close()
