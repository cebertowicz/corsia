"""Binding-rule configuration preserves its caller's declared transaction."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest


def _rows(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute("SELECT value,enabled FROM connection_rules ORDER BY value").fetchall()


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.05)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture
def rules(isolated_db):
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('rule-owner','test-user','t','t')"
        )
        db.execute(
            "INSERT INTO connection_rules "
            "(id,connector,fact,value,destination_thread_id,created_at,updated_at) "
            "VALUES ('seed','gmail','from','seed','rule-owner','t','t')"
        )
        db.commit()
    return isolated_db


def _change(operation, *, db=None):
    from connections import binder

    if operation == "create":
        return binder.create_rule(
            connector="gmail", fact="from", value="target",
            destination_thread_id="rule-owner", precedence=17, db=db,
        )
    if operation == "disable":
        return binder.disable_rule("seed", db=db)
    raise AssertionError(operation)


@pytest.mark.parametrize("operation", ["create", "disable"])
def test_standalone_rule_write_refuses_unknown_owner(rules, operation):
    from infra.database import TransactionOwnershipError, get_db

    db = get_db()
    db.execute("UPDATE connection_rules SET value='outer' WHERE id='seed'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _change(operation)
        assert db.in_transaction
        assert _rows(rules) == [("seed", 1)]
        db.commit()
        assert _rows(rules) == [("outer", 1)]
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("operation", ["create", "disable"])
def test_outer_rollback_reverts_rule_mutation(rules, operation):
    from infra.database import write_transaction

    with pytest.raises(ValueError, match="abort outer"):
        with write_transaction(None, scope="test_rule_owner") as db:
            _change(operation, db=db)
            raise ValueError("abort outer")
    assert _rows(rules) == [("seed", 1)]
    _writer_available(rules)


@pytest.mark.parametrize("operation,event", [("create", "INSERT"), ("disable", "UPDATE OF enabled")])
def test_rule_statement_failure_preserves_outer_work(rules, operation, event):
    from infra.database import write_transaction

    with closing(sqlite3.connect(str(rules))) as db:
        db.execute(
            f"CREATE TRIGGER reject_rule AFTER {event} ON connection_rules "
            "BEGIN SELECT RAISE(FAIL, 'rule rejected'); END"
        )
        db.commit()
    with write_transaction(None, scope="test_rule_owner") as db:
        db.execute("UPDATE connection_rules SET value='outer' WHERE id='seed'")
        with pytest.raises(sqlite3.IntegrityError, match="rule rejected"):
            _change(operation, db=db)
        assert db.in_transaction
        assert [tuple(row) for row in db.execute(
            "SELECT value,enabled FROM connection_rules ORDER BY value"
        )] == [("outer", 1)]
    assert _rows(rules) == [("outer", 1)]


@pytest.mark.parametrize("operation", ["create", "disable"])
def test_failed_rule_commit_closes_and_releases_writer(rules, monkeypatch, operation):
    from connections import binder
    from infra import database

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("rule commit failed")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(rules), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(binder, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="rule commit failed"):
            _change(operation)
        assert db.closed
        assert _rows(rules) == [("seed", 1)]
        _writer_available(rules)
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["create", "disable"])
def test_standalone_rule_result_is_durable(rules, operation):
    result = _change(operation)
    if operation == "create":
        assert (result.connector, result.fact, result.value, result.destination_thread_id,
                result.precedence, result.enabled) == ("gmail", "from", "target", "rule-owner", 17, True)
        assert _rows(rules) == [("seed", 1), ("target", 1)]
    else:
        assert result is None
        assert _rows(rules) == [("seed", 0)]
    _writer_available(rules)


def test_rule_validation_rejects_before_touching_caller_work(rules):
    from connections import binder
    from infra.database import get_db

    db = get_db()
    db.execute("UPDATE connection_rules SET value='outer' WHERE id='seed'")
    try:
        with pytest.raises(binder.StaleRuleFieldError):
            binder.create_rule(connector="gmail", fact="undeclared", value="target",
                               destination_thread_id="rule-owner")
        assert db.in_transaction
        assert _rows(rules) == [("seed", 1)]
        db.commit()
        assert _rows(rules) == [("outer", 1)]
    finally:
        db.rollback()
        db.close()
