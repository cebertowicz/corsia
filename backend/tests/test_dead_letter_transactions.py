"""Dead-letter persistence must not commit or damage a caller's write unit."""

from contextlib import closing
import json
import sqlite3

import pytest

from automation import watches
from events import dead_letter
from infra import database
from registry import models
from tests.event_routing_helpers import mk_session


@pytest.fixture
def parking_db(isolated_db):
    with database.write_transaction(None, scope="dead_letter_test_seed") as db:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('parking-project','Original','t','t')",
        )
    return isolated_db


@pytest.fixture
def caller_db(parking_db):
    with closing(sqlite3.connect(str(parking_db))) as db:
        db.row_factory = sqlite3.Row
        yield db


def _park(*, db=None, payload=None):
    return dead_letter.park(
        "test.unroutable", {"key": "value"} if payload is None else payload,
        "no_owner", "artifact-1", db=db,
    )


def _snapshot(path):
    with closing(sqlite3.connect(str(path))) as db:
        name = db.execute(
            "SELECT name FROM projects WHERE id='parking-project'",
        ).fetchone()[0]
        rows = db.execute(
            "SELECT id,internal_event,external_id,reason,payload_json,status "
            "FROM dead_letter_events ORDER BY id",
        ).fetchall()
        return name, rows


def _assert_writer_free(path):
    with closing(sqlite3.connect(str(path), timeout=0)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _reject_insert():
    with database.write_transaction(None, scope="dead_letter_test_fault") as db:
        db.execute(
            "CREATE TRIGGER reject_dead_letter AFTER INSERT ON dead_letter_events "
            "BEGIN SELECT RAISE(FAIL, 'parking write rejected'); END",
        )


@pytest.mark.parametrize("named", [False, True])
def test_parking_commits_its_own_row(parking_db, caller_db, named):
    row_id = _park(db=caller_db if named else None)

    name, rows = _snapshot(parking_db)
    assert name == "Original"
    assert rows == [
        (row_id, "test.unroutable", "artifact-1", "no_owner",
         json.dumps({"key": "value"}), "parked"),
    ]
    assert row_id
    assert caller_db.execute("SELECT 1").fetchone()[0] == 1
    _assert_writer_free(parking_db)


def test_named_pending_caller_keeps_rollback_authority(parking_db, caller_db):
    caller_db.execute("UPDATE projects SET name='Caller' WHERE id='parking-project'")
    assert _park(db=caller_db)
    assert caller_db.execute("SELECT count(*) FROM dead_letter_events").fetchone()[0] == 1
    assert _snapshot(parking_db) == ("Original", [])

    caller_db.rollback()
    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)


def test_outer_failure_rolls_back_parking_and_the_caller(parking_db, caller_db):
    with pytest.raises(ValueError, match="outer failed"):
        with database.write_transaction(caller_db, scope="parking_caller") as db:
            db.execute("UPDATE projects SET name='Caller' WHERE id='parking-project'")
            assert _park(db=db)
            raise ValueError("outer failed")

    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)


def test_anonymous_parking_does_not_consume_a_foreign_owner(parking_db):
    with pytest.raises(ValueError, match="outer failed"):
        with database.write_transaction(None, scope="unrelated_caller") as db:
            db.execute("UPDATE projects SET name='Caller' WHERE id='parking-project'")
            assert _park() == ""
            assert db.in_transaction
            assert db.execute("SELECT count(*) FROM dead_letter_events").fetchone()[0] == 0
            raise ValueError("outer failed")

    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)


@pytest.mark.parametrize("named", [False, True])
def test_failed_commit_clears_only_the_parking_unit(parking_db, monkeypatch, named):
    class FailCommit(database._NoOpClose):
        def commit(self):
            raise sqlite3.OperationalError("parking commit rejected")

    failing = FailCommit(database.get_db())
    monkeypatch.setattr(database, "get_db", lambda: failing)
    monkeypatch.setattr(dead_letter, "get_db", lambda: failing)

    assert _park(db=failing if named else None) == ""
    assert not failing.in_transaction
    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)


def test_partial_sql_failure_cannot_leak_into_the_caller_commit(parking_db, caller_db):
    _reject_insert()
    with database.write_transaction(caller_db, scope="parking_caller") as db:
        db.execute("UPDATE projects SET name='Caller' WHERE id='parking-project'")
        assert _park(db=db) == ""
        assert db.in_transaction
        assert db.execute("SELECT count(*) FROM dead_letter_events").fetchone()[0] == 0

    assert _snapshot(parking_db) == ("Caller", [])
    _assert_writer_free(parking_db)


def test_standalone_sql_failure_returns_empty_without_a_partial_row(parking_db):
    _reject_insert()
    assert _park() == ""
    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)


@pytest.mark.parametrize("named", [False, True])
def test_unserializable_payload_preserves_the_error_contract(parking_db, caller_db, named):
    assert _park(db=caller_db if named else None, payload={"bad": {1}}) == ""
    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)


def test_watch_expiry_and_dead_letter_roll_back_together(parking_db, caller_db):
    owner = mk_session("watch-owner")
    models.register_artifact(
        integration="local", type="user-raise", external_id="missing-artifact",
        parent_thread_id=owner,
    )
    watch = watches.arm_watch(
        "missing-artifact", {"type": "deadline", "value": "2026-06-27T09:00:00Z"},
    )
    with database.write_transaction(None, scope="remove_test_artifact") as db:
        db.execute("DELETE FROM artifact_identities WHERE external_id='missing-artifact'")

    with pytest.raises(ValueError, match="watch caller failed"):
        with database.write_transaction(caller_db, scope="watch_caller") as db:
            result = watches.expire_watch(watch.id, db=db)
            assert result.resolution == "dead_letter"
            assert result.routed_wakes == 1
            raise ValueError("watch caller failed")

    row = caller_db.execute(
        "SELECT status,routed_at FROM expected_event_watches WHERE id=?", (watch.id,),
    ).fetchone()
    assert tuple(row) == ("armed", None)
    assert _snapshot(parking_db) == ("Original", [])
    _assert_writer_free(parking_db)
