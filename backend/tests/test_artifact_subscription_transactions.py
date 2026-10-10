"""Subscription checks and writes share one declared transaction owner."""

import sqlite3
from contextlib import closing

import pytest

from infra import database
from registry import artifact_record as AR
from tests.event_routing_helpers import mk_session

IDENTITY = {"integration": "gmail", "type": "email-thread", "external_id": "subscription-1"}


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        title = db.execute("SELECT title FROM threads WHERE id='owner'").fetchone()
        identities = db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id",
        ).fetchall()
        relationships = db.execute(
            "SELECT artifact_id,parent_thread_id,relationship_state "
            "FROM artifact_registrations ORDER BY parent_thread_id",
        ).fetchall()
        return title, identities, relationships


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.02)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture(params=["subscribe", "remove"])
def subscription_case(request, isolated_db):
    mk_session("owner")
    mk_session("parent")
    AR.register_or_resolve(IDENTITY, owner_intent="owner")
    if request.param == "remove":
        AR.subscribe_parent_chain(IDENTITY, "parent")
    operation = (AR.subscribe_parent_chain if request.param == "subscribe"
                 else AR.remove_subscription)
    return isolated_db, operation, request.param


def test_subscription_does_not_commit_a_named_outer_owner(subscription_case):
    path, operation, _ = subscription_case
    before = _stored(path)
    with pytest.raises(RuntimeError, match="caller abort"):
        with database.write_transaction(None, scope="subscription_caller") as db:
            db.execute("UPDATE threads SET title='caller' WHERE id='owner'")
            operation(IDENTITY, "parent", db=db)
            assert db.in_transaction
            assert _stored(path) == before
            raise RuntimeError("caller abort")
    assert _stored(path) == before
    _writer_available(path)


def test_subscription_refuses_an_anonymous_foreign_owner(subscription_case):
    path, operation, _ = subscription_case
    before = _stored(path)
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='owner'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            operation(IDENTITY, "parent")
        assert db.in_transaction
        assert _stored(path) == before
        db.commit()
        assert _stored(path) == (("caller",), before[1], before[2])
    finally:
        db.rollback()
        db.close()


def test_relationship_checks_exclude_a_competing_writer(subscription_case):
    path, operation, action = subscription_case
    protected = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "SELECT * FROM artifact_identities" in sql:
                try:
                    _writer_available(path)
                except sqlite3.OperationalError as error:
                    assert "locked" in str(error)
                    protected.append(True)
                else:
                    protected.append(False)
            return super().execute(sql, *args, **kwargs)

    with closing(sqlite3.connect(str(path), factory=ObservedConnection)) as db:
        db.row_factory = sqlite3.Row
        record = operation(IDENTITY, "parent", db=db)
        assert protected and all(protected)
        assert record.kind_of("parent") == ("parent_chain" if action == "subscribe" else None)
        assert record.owner_thread == "owner" and record.lifecycle == "active"
        assert not db.in_transaction
        _writer_available(path)


def test_failed_subscription_sql_rolls_back_only_its_named_unit(subscription_case):
    path, operation, action = subscription_case
    before = _stored(path)
    event = "INSERT" if action == "subscribe" else "DELETE"
    row = "NEW" if action == "subscribe" else "OLD"
    with database.write_transaction(None, scope="subscription_fault_seed") as db:
        db.execute(
            f"CREATE TRIGGER reject_subscription AFTER {event} ON artifact_registrations "
            f"WHEN {row}.parent_thread_id='parent' "
            "BEGIN SELECT RAISE(FAIL, 'subscription rejected'); END",
        )
    with database.write_transaction(None, scope="subscription_caller") as db:
        db.execute("UPDATE threads SET title='caller' WHERE id='owner'")
        with pytest.raises(sqlite3.IntegrityError, match="subscription rejected"):
            operation(IDENTITY, "parent", db=db)
        assert db.in_transaction
        # FAIL preserves the statement's earlier effects unless our unit rolls
        # them back. Committing the caller must retain only its own title edit.
    assert _stored(path) == (("caller",), before[1], before[2])
    _writer_available(path)


@pytest.mark.parametrize("named_connection", [False, True])
def test_subscription_commit_failure_leaves_no_partial_write(
    subscription_case, monkeypatch, named_connection,
):
    path, operation, _ = subscription_case
    before = _stored(path)

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("subscription commit rejected")

    with closing(sqlite3.connect(str(path), factory=FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        monkeypatch.setattr(database, "get_db", lambda: db)
        monkeypatch.setattr(AR, "get_db", lambda: db)
        kwargs = {"db": db} if named_connection else {}
        with pytest.raises(sqlite3.OperationalError, match="subscription commit rejected"):
            operation(IDENTITY, "parent", **kwargs)
        if named_connection:
            assert not db.in_transaction
            assert db.execute("SELECT 1").fetchone()[0] == 1
        else:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                db.execute("SELECT 1")
        assert _stored(path) == before
        _writer_available(path)


def test_named_clean_subscription_is_durable_and_idempotent(subscription_case):
    path, operation, action = subscription_case
    with closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        first = operation(IDENTITY, "parent", db=db)
        second = operation(IDENTITY, "parent", db=db)
        assert first == second
        assert first.owner_thread == "owner" and first.lifecycle == "active"
        assert first.kind_of("parent") == ("parent_chain" if action == "subscribe" else None)
        assert not db.in_transaction
        assert db.execute("SELECT 1").fetchone()[0] == 1
    _, identities, relationships = _stored(path)
    assert len(identities) == 1 and identities[0][1] == "active"
    assert [(row[1], row[2]) for row in relationships] == (
        [("owner", "owner-active"), ("parent", "subscriber-parent-chain")]
        if action == "subscribe" else [("owner", "owner-active")]
    )
