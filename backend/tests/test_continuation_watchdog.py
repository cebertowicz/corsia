"""T1 watchdog budget, due-cycle, and append-only audit tests."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from automation.continuation import recovery, runner, triggers
from automation.continuation.sources.watchdog import WatchdogSource
from infra.schema import SCHEMA_SQL
from plans import admission, service


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('owner','user','t','t')"
    )
    db.commit()
    return db


def _active_node(db, *, budget=60):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Watchdog work",
        acceptance_criteria="The watchdog tests pass",
        watchdog_budget_seconds=budget,
        db=db,
    )
    # The watchdog measures from selection, so activation and selection share one clock.
    with patch(
        "plans.service._now",
        return_value="2000-01-01T00:00:00+00:00",
    ):
        active = service.transition_node(node.id, "active", db=db)
        admission.select(
            active.id, actor_id="owner", provenance="test", db=db, touch=False,
        )
    db.commit()
    return plan, active


class _LandedDelivery:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args) -> bool:
        self.calls += 1
        return True


class _CrashAfterWatchdogAudit(WatchdogSource):
    def settle(self, trigger, outcome, reason, *, db) -> None:
        super().settle(trigger, outcome, reason, db=db)
        raise RuntimeError("crash after watchdog audit")


class _BlockAfterCollection(WatchdogSource):
    """Create the production race after watchdog projection, before FIFO admission."""

    def __init__(self, node_id: str, blocker_id: str) -> None:
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


async def _find_landed_input(_thread_id, _idempotency_key):
    return {"delivery_state": "delivered"}


def test_watchdog_is_due_only_after_budget_and_transition_clears_it():
    db = _db()
    _plan, node = _active_node(db)
    source = WatchdogSource()

    before = source.due("2000-01-01T00:00:59+00:00", db=db)
    after = source.due("2000-01-01T00:01:01+00:00", db=db)
    service.transition_node(node.id, "failed", db=db)
    cleared = source.due("2099-01-01T00:00:00+00:00", db=db)

    assert before == []
    assert len(after) == 1
    assert after[0].subject_id == node.id
    assert cleared == []


@pytest.mark.asyncio
async def test_watchdog_blocked_after_collection_never_enters_fifo(isolated_db):
    from infra.database import get_db
    from tests.event_routing_helpers import mk_session

    mk_session("owner")
    db = get_db()
    plan, active = _active_node(db)
    blocker = service.add_node(
        plan.id,
        kind="task",
        title="Required first",
        acceptance_criteria="The prerequisite is accepted",
        db=db,
    )
    drive = AsyncMock(return_value=None)

    with patch("infra.send_service._deliver_admission", drive):
        result = await runner.sweep(
            "2000-01-01T00:01:01+00:00",
            db=db,
            sources=[_BlockAfterCollection(active.id, blocker.id)],
        )

    assert result["cancelled"] == ["owner"]
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='owner'"
    ).fetchone()[0] == 0
    trigger = db.execute(
        "SELECT state,reason FROM continuation_triggers WHERE kind='t1_watchdog'"
    ).fetchone()
    assert tuple(trigger) == ("cancelled", "stale_source_state")
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events "
        "WHERE node_id=? AND event='watchdog_cancelled'",
        (active.id,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events "
        "WHERE node_id=? AND event='watchdog_wake'",
        (active.id,),
    ).fetchone()[0] == 0
    drive.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_post_delivery_crash_recovers_trigger_and_watchdog_audit_atomically():
    db = _db()
    _plan, node = _active_node(db)
    delivery = _LandedDelivery()
    crash_source = _CrashAfterWatchdogAudit()

    with pytest.raises(RuntimeError, match="crash after watchdog audit"):
        await runner.sweep(
            "2000-01-01T00:01:01+00:00",
            db=db,
            sources=[crash_source],
            deliver=delivery,
        )

    trigger = db.execute(
        "SELECT * FROM continuation_triggers WHERE kind='t1_watchdog'"
    ).fetchone()
    assert delivery.calls == 1
    assert trigger["state"] == "dispatched"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE event='watchdog_wake'"
    ).fetchone()[0] == 0

    with pytest.raises(RuntimeError, match="crash after watchdog audit"):
        await recovery.recover_dispatched(
            db=db,
            find_input=_find_landed_input,
            sources=[crash_source],
            now="2000-01-01T00:02:00+00:00",
        )

    assert triggers.get(trigger["id"], db=db).state == "dispatched"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE event='watchdog_wake'"
    ).fetchone()[0] == 0

    recovered = await recovery.recover_dispatched(
        db=db,
        find_input=_find_landed_input,
        sources=[WatchdogSource()],
        now="2000-01-01T00:03:00+00:00",
    )
    repeated = await recovery.recover_dispatched(
        db=db,
        find_input=_find_landed_input,
        sources=[WatchdogSource()],
        now="2000-01-01T00:04:00+00:00",
    )

    assert recovered == {"delivered": 1, "retried": 0}
    assert repeated == {"delivered": 0, "retried": 0}
    assert delivery.calls == 1
    assert triggers.get(trigger["id"], db=db).state == "delivered"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE event='watchdog_wake'"
    ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_watchdog_keeps_waking_past_three_cycles_with_one_trigger_each():
    db = _db()
    _plan, node = _active_node(db)
    delivery = _LandedDelivery()

    for offset in range(4):
        await runner.sweep(
            f"2099-01-01T00:0{offset}:00+00:00",
            db=db, sources=[WatchdogSource()], deliver=delivery,
        )

    assert delivery.calls == 4
    assert db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE kind='t1_watchdog'"
    ).fetchone()[0] == 4
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE event='watchdog_wake'"
    ).fetchone()[0] == 4
