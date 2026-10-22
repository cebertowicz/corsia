"""Actor-bound transactional plan watches and T2 expiry tests."""

from __future__ import annotations

import sqlite3
from unittest.mock import Mock

import pytest

from automation.continuation.sources.watch_expiry import WatchExpirySource
from automation.continuation.triggers import Trigger
from infra.schema import SCHEMA_SQL
import plans.admission as admission
import plans.boundary as boundary
import plans.public as public
import plans.receipts as receipts
import plans.service as service
import plans.status as status
from plans.boundary import PlanActor


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    for thread_id in ("owner", "other"):
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?,'user','t','t')",
            (thread_id,),
        )
    db.commit()
    return db


def _plan(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Wait for external event",
        acceptance_criteria="The event resolves or expires",
        db=db,
    )
    other = service.add_node(
        plan.id,
        kind="task",
        title="Other node",
        acceptance_criteria="The other work completes",
        db=db,
    )
    node = service.transition_node(node.id, "active", db=db)
    return plan, node, other


def _actor():
    return PlanActor("owner", "user", None)


def _arm(db, plan, node, mutation_id="arm"):
    return boundary.mutate(
        plan.id,
        "arm_watch",
        {
            "node_id": node.id,
            "expected_revision": node.revision,
            "condition": {
                "type": "deadline",
                "value": "2000-01-01T00:00:00+00:00",
            },
            "expected_event": "external.done",
        },
        actor=_actor(),
        provenance="test",
        mutation_id=mutation_id,
        db=db,
    ).result


def test_plan_watch_arm_and_receipt_are_atomic_and_replay_safe(monkeypatch):
    db = _db()
    plan, node, _other = _plan(db)
    first = _arm(db, plan, node)
    replay = _arm(db, plan, node)

    assert replay == first
    assert db.execute(
        "SELECT COUNT(*) FROM expected_event_watches"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts"
    ).fetchone()[0] == 1

    original = receipts.record
    monkeypatch.setattr(
        receipts,
        "record",
        Mock(side_effect=RuntimeError("receipt failure")),
    )
    second = service.add_node(
        plan.id,
        kind="task",
        title="Rollback watch",
        acceptance_criteria="No partial watch remains",
        db=db,
    )
    second = service.transition_node(second.id, "active", db=db)
    with pytest.raises(RuntimeError, match="receipt failure"):
        _arm(db, plan, second, mutation_id="arm-rollback")
    monkeypatch.setattr(receipts, "record", original)

    assert db.execute(
        "SELECT COUNT(*) FROM expected_event_watches"
    ).fetchone()[0] == 1
    assert db.in_transaction is False


def test_watch_resolution_is_bound_to_plan_node_and_current_actor():
    db = _db()
    plan, node, other = _plan(db)
    watch = _arm(db, plan, node)["watch"]
    other = service.transition_node(other.id, "active", db=db)

    with pytest.raises(PermissionError):
        boundary.mutate(
            plan.id,
            "resolve_watch",
            {
                "node_id": other.id,
                "watch_id": watch["id"],
                "expected_revision": other.revision,
            },
            actor=_actor(),
            provenance="test",
            mutation_id="wrong-node",
            db=db,
        )

    result = boundary.mutate(
        plan.id,
        "resolve_watch",
        {
            "node_id": node.id,
            "watch_id": watch["id"],
            "expected_revision": node.revision,
        },
        actor=_actor(),
        provenance="test",
        mutation_id="resolve",
        db=db,
    ).result
    assert result["watch"]["status"] == "resolved"
    assert db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='owner'"
    ).fetchone()["status"] == "armed"


def _expiry_trigger(db) -> tuple[WatchExpirySource, Trigger]:
    """Return the expiry source and the delivered trigger for the one due watch."""
    source = WatchExpirySource()
    request = source.due("2099-01-01T00:00:00+00:00", db=db)[0]
    trigger = Trigger(
        id="t2",
        kind=request.kind,
        thread_id=request.thread_id,
        scope_id=request.scope_id,
        subject_id=request.subject_id,
        dedupe_key=request.dedupe_key,
        idempotency_key=None,
        state="delivered",
        reason="watch_expired",
        attempt=1,
        deferrals=0,
        cost_estimate_usd=0.0,
        next_attempt_at=None,
        created_at="t",
        updated_at="t",
    )
    return source, trigger


def _open_attempt(db, node_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


def test_late_watch_refires_once_and_plan_stays_waiting_until_settle():
    db = _db()
    plan, node, other = _plan(db)
    service.transition_node(other.id, "active", db=db)
    service.transition_node(other.id, "closed", db=db)
    watch = _arm(db, plan, node)["watch"]
    source = WatchExpirySource()

    requests = source.due("2099-01-01T00:00:00+00:00", db=db)

    assert [(request.kind, request.thread_id) for request in requests] == [("t2_watch_expiry", "owner")]
    assert "2000-01-01T00:00:00+00:00" in requests[0].details["summary"]
    assert "arm_watch again" in requests[0].details["summary"]
    assert status.read(db, service._plan_row(db, plan.id), actor=_actor())["severity"] == "waiting"
    source.settle(_expiry_trigger(db)[1], "delivered", "watch_expired", db=db)
    assert source.due("2099-01-01T00:00:00+00:00", db=db) == []
    assert db.execute(
        "SELECT status FROM expected_event_watches WHERE id=?", (watch["id"],)
    ).fetchone()["status"] == "expired"


def test_only_a_wake_past_the_grace_names_the_missed_time():
    due = "2000-01-01T00:00:00+00:00"

    assert public.late_wake(due, "2000-01-01T00:05:00+00:00") is None
    assert public.late_wake(due, "2000-01-01T00:11:00+00:00") is None
    late = public.late_wake(due, "2000-01-01T00:11:01+00:00")
    assert due in late and "arm_watch it again" in late
    assert public.late_wake(None, "2000-01-01T01:00:00+00:00") is None


def test_t2_expiry_routes_once_and_rearms_t0():
    db = _db()
    plan, node, _other = _plan(db)
    watch = _arm(db, plan, node)["watch"]
    source, trigger = _expiry_trigger(db)

    source.settle(trigger, "delivered", "watch_expired", db=db)
    source.settle(trigger, "delivered", "watch_expired", db=db)

    assert db.execute(
        "SELECT status FROM expected_event_watches WHERE id=?", (watch["id"],)
    ).fetchone()["status"] == "expired"
    assert db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='owner'"
    ).fetchone()["status"] == "armed"
    # Arming released the executor slot; expiry has to hand it back, or the
    # task is eligible, selected by nobody, and the plan stalls unwoken.
    projected = admission.project(node.id, db=db)
    assert projected["condition"] == "running"
    assert projected["selection"]["executor_thread_id"] == "owner"


def test_t2_expiry_with_an_empty_slot_selects_the_first_task_in_plan_order():
    """Expiry asks Plans to reconcile the watch's plan, not to reselect its node.

    An earlier task became active and queued while the watch waited, and the
    executor's slot is empty, so canonical plan order selects that task.
    """
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    first = service.add_node(
        plan.id, kind="task", title="Earlier queued work",
        acceptance_criteria="The earlier work completes", db=db,
    )
    waiter = service.add_node(
        plan.id, kind="task", title="Wait for external event",
        acceptance_criteria="The event resolves or expires", db=db,
    )
    waiter = service.transition_node(waiter.id, "active", db=db)
    _arm(db, plan, waiter)
    service.transition_node(first.id, "active", db=db)
    assert _open_attempt(db, first.id) is None
    source, trigger = _expiry_trigger(db)

    source.settle(trigger, "delivered", "watch_expired", db=db)

    assert _open_attempt(db, first.id) is not None
    assert _open_attempt(db, waiter.id) is None


def test_t2_expiry_never_preempts_a_valid_selection():
    db = _db()
    plan, waiter, holder = _plan(db)
    _arm(db, plan, waiter)
    selected = admission.activate(
        holder.id, expected_revision=holder.revision, actor_id="owner",
        provenance="test", db=db,
    )
    db.commit()
    holder_attempt = selected["admission"]["selection"]["attempt_id"]
    source, trigger = _expiry_trigger(db)

    source.settle(trigger, "delivered", "watch_expired", db=db)

    assert _open_attempt(db, holder.id)["id"] == holder_attempt
    assert _open_attempt(db, waiter.id) is None
