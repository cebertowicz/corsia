"""Admission read shortcuts retain transaction and schema boundaries."""

import sqlite3

import pytest

from infra.database import write_transaction
from infra.db_connection import _bulk_authorizer
from plans import aggregates
from plans.wait_causes import account_read_snapshot
from tests.plan_writer_authorizer import authorize


@pytest.mark.parametrize("rollback", [False, True])
def test_schema_readiness_is_fresh_after_each_admission(rollback):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE plan_nodes (id TEXT)")
    db.commit()
    sql = []
    db.set_trace_callback(sql.append)
    if rollback:
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope="admit_input"):
                assert not aggregates.ready(db)
                assert not aggregates.ready(db)
                raise RuntimeError("rollback")
    else:
        with write_transaction(db, scope="admit_input"):
            assert not aggregates.ready(db)
            assert not aggregates.ready(db)
    assert sum(s == "PRAGMA schema_version" for s in sql) == 1
    db.execute("ALTER TABLE plan_nodes ADD COLUMN sub_open INTEGER")
    db.commit()
    with write_transaction(db, scope="admit_input"):
        assert aggregates.ready(db)
    assert sum(s == "PRAGMA schema_version" for s in sql) == 2
    db.close()


def test_account_snapshot_kept_in_autocommit_and_skipped_in_transaction():
    db = sqlite3.connect(":memory:")
    statements = []
    db.set_trace_callback(statements.append)
    with account_read_snapshot(db):
        assert db.in_transaction
        db.execute("SELECT 1").fetchall()
    assert any(s.startswith("SAVEPOINT plan_account_read") for s in statements)
    statements.clear()
    db.execute("BEGIN IMMEDIATE")
    with account_read_snapshot(db):
        db.execute("SELECT 1").fetchall()
    assert not any("plan_account_read" in s for s in statements)
    db.rollback()
    db.close()


@pytest.mark.parametrize("authorizer", [_bulk_authorizer, authorize])
def test_in_tree_authorizers_allow_snapshot_and_schema_version(authorizer):
    assert authorizer(sqlite3.SQLITE_SAVEPOINT, "BEGIN", "plan_account_read", None, None) == sqlite3.SQLITE_OK
    assert authorizer(sqlite3.SQLITE_PRAGMA, "schema_version", None, None, None) == sqlite3.SQLITE_OK
