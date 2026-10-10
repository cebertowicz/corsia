"""A passive artifact change respects the transaction that owns its journal."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, nullcontext
from uuid import uuid4

import pytest

from infra.database import TransactionOwnershipError, get_db, write_transaction
from registry import attention, models
from tests.registry_helpers import seed_artifact


def _seed(db):
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
        "VALUES ('journal-owner',?,'Original','native/test','t','t')",
        (str(uuid4()),),
    )
    seed_artifact(
        db, artifact_id="journal-artifact", integration="test", type="user-raise",
        external_id="journal-item", parent_thread_id="journal-owner",
    )
    db.execute(
        "UPDATE artifact_identities SET metadata=? WHERE id='journal-artifact'",
        ('{"attention_state":"deferred"}',),
    )
    db.commit()


def _record(db=None):
    return models.record_artifact_change(
        artifact_id="journal-artifact", internal_event="artifact.changed",
        external_id="journal-item", summary="Evidence changed",
        provenance={"delivery": "digest", "wake": False},
        actor_thread_id="journal-owner", db=db,
    )


def _journal(db):
    return [tuple(row) for row in db.execute(
        "SELECT object_kind,object_id,change_kind,summary,details_json,actor_thread_id "
        "FROM context_change_journal ORDER BY seq"
    )]


def test_implicit_journal_write_preserves_foreign_work(isolated_db):
    db = get_db()
    _seed(db)
    db.execute("UPDATE threads SET title='Pending' WHERE id='journal-owner'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _record()
        assert db.in_transaction
        assert db.execute("SELECT title FROM threads WHERE id='journal-owner'").fetchone()[0] == "Pending"
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert _journal(observer) == []
            assert observer.execute("SELECT title FROM threads WHERE id='journal-owner'").fetchone()[0] == "Original"
    finally:
        db.rollback()


def test_named_journal_write_obeys_outer_rollback(isolated_db):
    db = get_db()
    _seed(db)
    with pytest.raises(RuntimeError, match="abort caller"):
        with write_transaction(db, scope="test_journal_caller"):
            _record(db)
            assert db.in_transaction
            raise RuntimeError("abort caller")
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _journal(observer) == []


class _FailedCommit(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("journal commit rejected")
        return super().commit()


def test_failed_journal_commit_rolls_back_and_releases_writer(isolated_db):
    with closing(sqlite3.connect(isolated_db, factory=_FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        _seed(db)
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="journal commit rejected"):
            _record(db)
        assert not db.in_transaction
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            assert _journal(observer) == []
            observer.execute("UPDATE threads SET title='Available' WHERE id='journal-owner'")
            observer.commit()


@pytest.mark.parametrize("borrowed", (False, True))
def test_failed_journal_insert_cannot_leak_into_a_later_commit(isolated_db, borrowed):
    db = get_db()
    _seed(db)
    db.execute(
        "CREATE TRIGGER reject_journal AFTER INSERT ON context_change_journal "
        "BEGIN SELECT RAISE(FAIL, 'journal insert rejected'); END"
    )
    db.commit()
    caller = write_transaction(db, scope="test_journal_caller") if borrowed else nullcontext(db)
    with caller:
        if borrowed:
            db.execute("UPDATE threads SET title='Caller retained' WHERE id='journal-owner'")
        with pytest.raises(sqlite3.IntegrityError, match="journal insert rejected"):
            _record(db)
        assert db.in_transaction is borrowed
        assert _journal(db) == []
    with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
        assert _journal(observer) == []
        title = observer.execute("SELECT title FROM threads WHERE id='journal-owner'").fetchone()[0]
        assert title == ("Caller retained" if borrowed else "Original")
        observer.execute("UPDATE threads SET title='Available' WHERE id='journal-owner'")
        observer.commit()


def test_journal_preserves_one_durable_change_and_its_provenance(isolated_db):
    db = get_db()
    _seed(db)
    result = _record()
    with closing(sqlite3.connect(isolated_db)) as observer:
        rows = _journal(observer)
        assert len(rows) == 1
        kind, object_id, event, summary, details, actor = rows[0]
        assert (kind, object_id, event, summary, actor) == (
            "artifact", "journal-artifact", "artifact.changed", "Evidence changed", "journal-owner",
        )
        assert json.loads(details) == {
            "external_id": "journal-item", "provenance": {"delivery": "digest", "wake": False},
        }
        sequence = observer.execute("SELECT seq FROM context_change_journal").fetchone()[0]
        assert result["seq"] == sequence


@pytest.mark.parametrize("consumer", ("attention", "watch"))
def test_real_consumer_settlement_and_journal_share_outer_rollback(isolated_db, consumer):
    from automation import watches

    db = get_db()
    _seed(db)
    watch = watches.arm_watch(
        "journal-artifact", {"type": "deadline", "value": "2030-01-01T00:00:00Z"},
        owner_thread="journal-owner", db=db,
    ) if consumer == "watch" else None
    with pytest.raises(RuntimeError, match="abort settlement"):
        with write_transaction(db, scope="test_journal_settlement"):
            if watch is None:
                result = attention.clear_attention(
                    "journal-artifact", outcome="dismiss", db=db,
                )
                assert result.attention_state == "cleared"
            else:
                result = watches.expire_watch(watch.id, db=db)
                assert result.routed_wakes == 1
            assert len(_journal(db)) == 1
            assert db.in_transaction
            raise RuntimeError("abort settlement")
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _journal(observer) == []
        metadata = observer.execute(
            "SELECT metadata FROM artifact_identities WHERE id='journal-artifact'"
        ).fetchone()[0]
        assert json.loads(metadata) == {"attention_state": "deferred"}
        if watch is not None:
            assert observer.execute(
                "SELECT status,routed_at FROM expected_event_watches WHERE id=?", (watch.id,),
            ).fetchone() == ("armed", None)
