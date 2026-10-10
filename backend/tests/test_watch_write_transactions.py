"""Watch lifecycle writes and their existing metric obey one declared owner."""

from contextlib import closing, nullcontext
import json
import sqlite3

import pytest

from automation import watches
from automation.continuation import settlement, triggers
from automation.continuation.sources.watch_expiry import WatchExpirySource
from infra.database import TransactionOwnershipError, get_db, write_transaction
from tests.registry_helpers import make_session, seed_artifact


NOW = "2026-09-14T00:00:00+00:00"
CONDITION = {"type": "deadline", "value": "2000-01-01T00:00:00+00:00"}
OPERATIONS = ("arm", "resolve", "expire", "sweep")


def _seed(db):
    owner = make_session(db, title="Original")
    seed_artifact(
        db, artifact_id="watch-artifact", integration="test", type="note",
        external_id="watch-item", parent_thread_id=owner,
        metadata='{"attention_state":"deferred"}',
    )
    db.commit()
    watch = watches.arm_watch("watch-artifact", CONDITION, db=db)
    trigger, _ = triggers.enqueue(
        WatchExpirySource().due(NOW, db=db)[0], db=db, now=NOW,
    )
    db.commit()
    return owner, watch, trigger


def _mutate(operation, watch, db=None):
    if operation == "arm":
        return watches.arm_watch("watch-artifact", CONDITION, expected_event="next", db=db)
    if operation == "resolve":
        return watches.resolve_watch(watch.id, db=db)
    if operation == "sweep":
        return watches.sweep_expired(NOW, db=db)
    return watches.expire_watch(watch.id, db=db)


def _unchanged(db):
    assert [tuple(row) for row in db.execute(
        "SELECT status,routed_at FROM expected_event_watches"
    )] == [("armed", None)]
    assert db.execute("SELECT state FROM continuation_triggers").fetchone()[0] == "pending"
    assert json.loads(db.execute("SELECT metadata FROM artifact_identities").fetchone()[0]) == {
        "attention_state": "deferred",
    }
    assert db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0


@pytest.mark.parametrize("operation", OPERATIONS)
def test_anonymous_watch_write_preserves_foreign_owner(isolated_db, operation):
    db = get_db()
    owner, watch, _ = _seed(db)
    db.execute("UPDATE threads SET title='Pending' WHERE id=?", (owner,))
    try:
        with pytest.raises(TransactionOwnershipError):
            _mutate(operation, watch)
        assert db.in_transaction
        assert db.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "Pending"
        with closing(sqlite3.connect(isolated_db)) as observer:
            _unchanged(observer)
            assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "Original"
    finally:
        db.rollback()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_named_watch_write_obeys_caller_rollback(isolated_db, operation):
    db = get_db()
    _, watch, _ = _seed(db)
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="watch_test_caller"):
            _mutate(operation, watch, db)
            assert db.in_transaction
            raise RuntimeError("caller failed")
    with closing(sqlite3.connect(isolated_db)) as observer:
        _unchanged(observer)


@pytest.mark.parametrize("borrowed", (False, True))
@pytest.mark.parametrize("operation", OPERATIONS)
def test_partial_watch_write_rolls_back_before_caller_continues(isolated_db, operation, borrowed):
    db = get_db()
    owner, watch, _ = _seed(db)
    table, verb = {
        "arm": ("expected_event_watches", "INSERT"),
        "resolve": ("continuation_triggers", "UPDATE"),
        "expire": ("artifact_identities", "UPDATE"),
        "sweep": ("artifact_identities", "UPDATE"),
    }[operation]
    db.execute(
        f"CREATE TRIGGER reject_watch AFTER {verb} ON {table} "
        "BEGIN SELECT RAISE(FAIL, 'watch write rejected'); END"
    )
    db.commit()
    caller = write_transaction(db, scope="watch_test_caller") if borrowed else nullcontext(db)
    with caller:
        if borrowed:
            db.execute("UPDATE threads SET title='Caller retained' WHERE id=?", (owner,))
        with pytest.raises(sqlite3.IntegrityError, match="watch write rejected"):
            _mutate(operation, watch, db)
        assert db.in_transaction is borrowed
        _unchanged(db)
    with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
        _unchanged(observer)
        assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == (
            "Caller retained" if borrowed else "Original"
        )
        observer.execute("UPDATE threads SET title='Writer released' WHERE id=?", (owner,))
        observer.commit()


class _FailedCommit(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("watch commit rejected")
        return super().commit()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_failed_watch_commit_releases_writer_without_partial_state(isolated_db, operation):
    with closing(sqlite3.connect(isolated_db, factory=_FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        owner, watch, _ = _seed(db)
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="watch commit rejected"):
            _mutate(operation, watch, db)
        assert not db.in_transaction
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            _unchanged(observer)
            observer.execute("UPDATE threads SET title='Writer released' WHERE id=?", (owner,))
            observer.commit()


def _capture_metrics(monkeypatch, db, path):
    observed = []

    def capture(category, action, *, properties):
        with closing(sqlite3.connect(path)) as observer:
            watch_status = observer.execute("SELECT status FROM expected_event_watches").fetchone()[0]
            trigger_state = observer.execute("SELECT state FROM continuation_triggers").fetchone()[0]
        observed.append((category, action, db.in_transaction, watch_status, trigger_state, properties))

    monkeypatch.setattr(watches, "emit_product_event", capture)
    monkeypatch.setattr(settlement, "emit_product_event", capture, raising=False)
    return observed


@pytest.mark.parametrize("composed", (False, True))
def test_expiry_metric_observes_committed_state_once(isolated_db, monkeypatch, composed):
    db = get_db()
    _, watch, trigger = _seed(db)
    observed = _capture_metrics(monkeypatch, db, isolated_db)
    if composed:
        triggers.claim(trigger.id, idempotency_key="expiry-test", db=db, now=NOW)
        db.commit()
        settled = settlement.settle_claimed(
            trigger.id, "delivered", "watch_expired", WatchExpirySource(), db=db, now=NOW,
        )
        assert settled.state == "delivered"
        settlement.settle_existing(settled, WatchExpirySource(), db=db)
    else:
        assert watches.expire_watch(watch.id, db=db).routed_wakes == 1
        assert watches.expire_watch(watch.id, db=db).already_final
    assert len(observed) == 1
    category, action, active, watch_status, trigger_state, properties = observed[0]
    assert (category, action, active, watch_status, trigger_state) == (
        "automation_watch", "expired", False, "expired", "delivered" if composed else "cancelled",
    )
    assert properties["watch_id"] == watch.id
    assert properties["resolution"] == "delivery_failure"
    assert db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 1


def test_failed_settlement_commit_emits_no_expiry_metric(isolated_db, monkeypatch):
    with closing(sqlite3.connect(isolated_db, factory=_FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        _, watch, trigger = _seed(db)
        triggers.claim(trigger.id, idempotency_key="expiry-test", db=db, now=NOW)
        db.commit()
        observed = _capture_metrics(monkeypatch, db, isolated_db)
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="watch commit rejected"):
            settlement.settle_claimed(
                trigger.id, "delivered", "watch_expired", WatchExpirySource(), db=db, now=NOW,
            )
        assert observed == []
        assert not db.in_transaction
        assert watches.get_watch(watch.id, db=db).status == "armed"
        assert triggers.get(trigger.id, db=db).state == "dispatched"
        assert db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0


def test_nested_settlement_emits_nothing_before_caller_rollback(isolated_db, monkeypatch):
    db = get_db()
    _, watch, trigger = _seed(db)
    triggers.claim(trigger.id, idempotency_key="expiry-test", db=db, now=NOW)
    db.commit()
    observed = _capture_metrics(monkeypatch, db, isolated_db)
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="watch_test_caller"):
            settlement.settle_claimed(
                trigger.id, "delivered", "watch_expired", WatchExpirySource(), db=db, now=NOW,
            )
            assert observed == []
            assert watches.get_watch(watch.id, db=db).status == "expired"
            raise RuntimeError("caller failed")
    assert observed == []
    assert watches.get_watch(watch.id, db=db).status == "armed"
    assert triggers.get(trigger.id, db=db).state == "dispatched"


def test_deferred_watch_emits_no_expiry_metric(isolated_db, monkeypatch):
    db = get_db()
    _, watch, trigger = _seed(db)
    observed = _capture_metrics(monkeypatch, db, isolated_db)
    assert settlement.defer_with_source(
        trigger, "thread_unavailable", "2026-09-15T00:00:00+00:00",
        WatchExpirySource(), db=db, now=NOW,
    )
    assert observed == []
    assert watches.get_watch(watch.id, db=db).status == "armed"
    assert triggers.get(trigger.id, db=db).state == "deferred"


def test_health_lookup_reads_named_owner_without_rollback(isolated_db):
    from events.health_sink import WORKSPACE_HEALTH_SINK, resolve_live_health_owner

    db = get_db()
    owner, _, _ = _seed(db)
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="watch_test_caller"):
            db.execute(
                "UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id=?",
                (owner,),
            )
            assert resolve_live_health_owner([owner], db=db) == WORKSPACE_HEALTH_SINK
            assert db.in_transaction
            assert db.execute("SELECT lifecycle_state FROM threads WHERE id=?", (owner,)).fetchone()[0] == "archived"
            raise RuntimeError("caller failed")
    assert resolve_live_health_owner([owner], db=db) == owner
