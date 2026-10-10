"""Plan wake writes respect their caller and survive failed persistence cleanly."""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
import sqlite3
from contextlib import closing

import pytest

from automation import plan_liveness
from infra.database import TransactionOwnershipError, get_db, write_transaction
from plans import boundary, projections, service
from plans.models import PlanLivenessSignal


OWNER = "wake-owner"
OPERATIONS = ("arm", "cancel", "complete", "external_complete")


def _seed(db, operation: str) -> str:
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
        "VALUES (?, 'fixture-user', 'Original', 't', 't')",
        (OWNER,),
    )
    db.commit()
    plan = service.ensure_plan("thread", OWNER, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Continue the selected work",
        acceptance_criteria="The selected work finishes", db=db,
    )
    boundary.mutate(
        plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=boundary.PlanActor(OWNER, "fixture-user", None),
        provenance="test.plan_liveness_transactions", mutation_id="activate", db=db,
    )
    if operation == "cancel":
        plan_liveness.on_turn_complete(OWNER, db=db)
    db.commit()
    return plan.id


def _apply(operation: str, db=None):
    if operation == "arm":
        return plan_liveness.arm_plan_wake(
            OWNER, PlanLivenessSignal(1, "selected-work", None, ()), db=db,
        )
    if operation == "cancel":
        return plan_liveness.cancel_plan_wake(OWNER, db=db)
    if operation == "complete":
        return plan_liveness.on_turn_complete(OWNER, db=db)
    return plan_liveness_terminal.on_thread_turn_complete(
        OWNER, db=db, effects=PendingTurnEndEffects(),
    )


def _wake_status(db):
    row = db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?", (OWNER,),
    ).fetchone()
    return row[0] if row else None


def _title(db):
    return db.execute("SELECT title FROM threads WHERE id=?", (OWNER,)).fetchone()[0]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_standalone_wake_refuses_foreign_pooled_work(isolated_db, operation):
    db = get_db()
    _seed(db, operation)
    before = "armed" if operation == "cancel" else None
    db.execute("UPDATE threads SET title='Caller pending' WHERE id=?", (OWNER,))
    try:
        with pytest.raises(TransactionOwnershipError):
            _apply(operation)
        assert db.in_transaction
        assert _title(db) == "Caller pending"
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert _title(observer) == "Original"
            assert _wake_status(observer) == before
    finally:
        db.rollback()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_wake_joins_explicit_caller_rollback(isolated_db, operation):
    db = get_db()
    _seed(db, operation)
    before = "armed" if operation == "cancel" else None
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="caller"):
            db.execute("UPDATE threads SET title='Caller pending' WHERE id=?", (OWNER,))
            result = _apply(operation, db)
            assert result is not None
            assert db.in_transaction
            assert _wake_status(db) == ("cancelled" if operation == "cancel" else "armed")
            with closing(sqlite3.connect(isolated_db)) as observer:
                assert _title(observer) == "Original"
                assert _wake_status(observer) == before
            raise RuntimeError("caller failed")
    assert not db.in_transaction
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _title(observer) == "Original"
        assert _wake_status(observer) == before


class _FailCommit(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("wake commit failed")
        return super().commit()


@pytest.mark.parametrize("operation", ("complete", "cancel"))
def test_failed_wake_commit_releases_writer(isolated_db, operation):
    with closing(sqlite3.connect(isolated_db, factory=_FailCommit)) as db:
        db.row_factory = sqlite3.Row
        _seed(db, operation)
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="wake commit failed"):
            _apply(operation, db)
        assert not db.in_transaction
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            assert _wake_status(observer) == ("armed" if operation == "cancel" else None)
            observer.execute("UPDATE threads SET title='Writer available' WHERE id=?", (OWNER,))
            observer.commit()
        db.fail_commit = False
        _apply(operation, db)
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert _wake_status(observer) == ("cancelled" if operation == "cancel" else "armed")


def test_signal_read_and_wake_write_share_one_boundary(isolated_db, monkeypatch):
    db = get_db()
    _seed(db, "complete")
    real_signal = projections.liveness_signal

    def read_signal(thread_id, *, db):
        assert db.in_transaction, "liveness must be read inside the wake's write boundary"
        with closing(sqlite3.connect(isolated_db, timeout=0)) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("UPDATE plans SET state='paused'")
        return real_signal(thread_id, db=db)

    monkeypatch.setattr(projections, "liveness_signal", read_signal)
    wake = plan_liveness.on_turn_complete(OWNER)
    assert wake is not None
    assert wake.last_open_count == 1
    assert not db.in_transaction
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _wake_status(observer) == "armed"
        assert observer.execute("SELECT state FROM plans").fetchone()[0] == "active"


def test_caught_wake_failure_cannot_leak_into_caller_commit(isolated_db):
    db = get_db()
    _seed(db, "complete")
    db.execute(
        "CREATE TRIGGER reject_wake AFTER INSERT ON plan_liveness_wakes BEGIN "
        "UPDATE plan_liveness_wakes SET cycle=99 WHERE id=NEW.id; "
        "SELECT RAISE(FAIL, 'wake insert failed'); END"
    )
    db.commit()
    with write_transaction(db, scope="caller"):
        db.execute("UPDATE threads SET title='Caller survives' WHERE id=?", (OWNER,))
        with pytest.raises(sqlite3.IntegrityError, match="wake insert failed"):
            plan_liveness.on_turn_complete(OWNER, db=db)
        assert db.in_transaction
        assert _wake_status(db) is None
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _title(observer) == "Caller survives"
        assert _wake_status(observer) is None
