"""Bounded progress classification and anti-storm regression coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from automation import plan_liveness as pl
from automation.continuation import runner
from automation.continuation.sources.liveness import LivenessSource
from automation.continuation.progress import (
    ADVANCED,
    CHURNED,
    UNCHANGED,
    ProgressSnapshot,
    classify_progress,
)
from infra.database import get_db
from plans import admission, mutations, service
from plans.models import PlanLivenessSignal
from tests.event_routing_helpers import mk_session


def _signal(count: int, digest: str, settled: int) -> PlanLivenessSignal:
    return PlanLivenessSignal(count, digest, "plan", (), settled)


class _BlockAfterCollection(LivenessSource):
    """Create the production race after due-state projection, before FIFO admission."""

    def __init__(self, node_id: str, blocker_id: str) -> None:
        super().__init__()
        self.node_id = node_id
        self.blocker_id = blocker_id

    def due(self, now: str, *, db):
        requests = super().due(now, db=db)
        current = service.get_node(self.node_id, db=db)
        service.add_dependency(
            self.node_id,
            self.blocker_id,
            expected_revision=current.revision,
            db=db,
        )
        return requests


@pytest.mark.parametrize(
    ("current", "snapshot", "expected"),
    [
        (_signal(1, "new", 2), ProgressSnapshot(2, "old", 2), ADVANCED),
        (_signal(2, "same", 3), ProgressSnapshot(2, "same", 2), ADVANCED),
        (_signal(2, "new", 2), ProgressSnapshot(2, "old", 2), CHURNED),
        (_signal(2, "same", 2), ProgressSnapshot(2, "same", 2), UNCHANGED),
    ],
)
def test_progress_classification(current, snapshot, expected):
    assert classify_progress(current, snapshot) == expected


@pytest.mark.asyncio
async def test_twelve_churn_rounds_keep_waking_at_a_widening_gap(isolated_db):
    thread_id = mk_session("bounded-churn")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Initial",
        acceptance_criteria="The churn test stops",
        db=db,
    )
    service.transition_node(node.id, "active", db=db)
    admission.select(
        node.id, actor_id=thread_id, provenance="test", db=db, touch=False,
    )
    pl.on_turn_complete(thread_id, db=db)
    deliver = AsyncMock(return_value=True)

    for round_number in range(12):
        current = service.get_node(node.id, db=db)
        mutations.edit_node(
            node.id,
            title=f"Churn {round_number}",
            expected_revision=current.revision,
            db=db,
        )
        db.execute(
            "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?",
            ("2000-01-01T00:00:00+00:00", thread_id),
        )
        db.commit()
        await pl.sweep_plan_wakes(
            "2099-01-01T00:00:00+00:00",
            db=db,
            deliver=deliver,
        )

    row = db.execute(
        "SELECT status,cycle FROM plan_liveness_wakes WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    assert (row["status"], row["cycle"]) == ("armed", 12)
    assert deliver.await_count == 12
    db.close()


@pytest.mark.asyncio
async def test_liveness_wake_blocked_after_collection_never_enters_fifo(isolated_db):
    thread_id = mk_session("stale-liveness")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    blocker = service.add_node(
        plan.id,
        kind="task",
        title="Required first",
        acceptance_criteria="The prerequisite is accepted",
        db=db,
    )
    active = service.add_node(
        plan.id,
        kind="task",
        title="Currently executable",
        acceptance_criteria="No stale wake is delivered",
        db=db,
    )
    active = service.transition_node(active.id, "active", db=db)
    admission.select(
        active.id, actor_id=thread_id, provenance="test", db=db, touch=False,
    )
    pl.on_turn_complete(thread_id, db=db)
    db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?",
        ("2000-01-01T00:00:00+00:00", thread_id),
    )
    db.commit()
    drive = AsyncMock(return_value=None)

    with patch("infra.send_service._deliver_admission", drive):
        result = await runner.sweep(
            "2099-01-01T00:00:00+00:00",
            db=db,
            sources=[_BlockAfterCollection(active.id, blocker.id)],
        )

    assert result["cancelled"] == [thread_id]
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=?",
        (thread_id,),
    ).fetchone()[0] == 0
    trigger = db.execute(
        "SELECT state,reason FROM continuation_triggers WHERE kind='t0_liveness'"
    ).fetchone()
    assert tuple(trigger) == ("cancelled", "stale_source_state")
    assert db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?",
        (thread_id,),
    ).fetchone()["status"] == pl.CANCELLED
    drive.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_failed_t0_delivery_stalls_the_wake_without_replaying_forever(isolated_db):
    thread_id = mk_session("failed-t0")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Active work",
        acceptance_criteria="The failed wake is terminal", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    admission.select(
        node.id, actor_id=thread_id, provenance="test", db=db, touch=False,
    )
    pl.on_turn_complete(thread_id, db=db)
    db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?",
        ("2000-01-01T00:00:00+00:00", thread_id),
    )
    db.commit()

    deliver = AsyncMock(return_value=False)
    await runner.sweep(
        "2099-01-01T00:00:00+00:00",
        db=db, sources=[LivenessSource()],
        deliver=deliver,
    )
    await runner.sweep(
        "2099-01-01T01:00:00+00:00",
        db=db, sources=[LivenessSource()],
        deliver=deliver,
    )

    assert deliver.await_count == 1
    assert db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE kind='t0_liveness'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?",
        (thread_id,),
    ).fetchone()["status"] == pl.STALLED
    db.close()
