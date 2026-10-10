"""Reviewed-SHA persistence cannot take ownership of unrelated pooled work."""

import sqlite3
from contextlib import closing

import pytest

from events import reviewer_gate_state
from infra import database


@pytest.fixture
def receipt_store(isolated_db):
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.executemany(
            "INSERT INTO reviewer_gate_state "
            "(pr_key,reviewed_sha) VALUES (?,?)",
            [("fixture/repo#1", "previous"), ("fixture/repo#2", "neighbor")],
        )
        db.commit()
    return isolated_db


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        return dict(db.execute(
            "SELECT pr_key,reviewed_sha FROM reviewer_gate_state",
        ).fetchall())


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.03)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def test_review_receipt_refuses_unrelated_pooled_transaction(receipt_store):
    db = database.get_db()
    db.execute(
        "INSERT INTO settings (key,value,updated_at) "
        "VALUES ('caller-owned','pending','t')",
    )
    try:
        with pytest.raises(database.TransactionOwnershipError):
            reviewer_gate_state.mark_reviewed("fixture/repo#1", "replacement")
        assert db.in_transaction
        assert _stored(receipt_store) == {
            "fixture/repo#1": "previous", "fixture/repo#2": "neighbor",
        }
        with closing(sqlite3.connect(str(receipt_store))) as observer:
            assert observer.execute(
                "SELECT value FROM settings WHERE key='caller-owned'",
            ).fetchone() is None
        db.commit()
        with closing(sqlite3.connect(str(receipt_store))) as observer:
            assert observer.execute(
                "SELECT value FROM settings WHERE key='caller-owned'",
            ).fetchone() == ("pending",)
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("pr_key", ["fixture/repo#1", "fixture/repo#3"])
def test_review_receipt_commits_only_exact_key(receipt_store, pr_key):
    reviewer_gate_state.mark_reviewed(pr_key, "replacement")
    expected = {"fixture/repo#1": "previous", "fixture/repo#2": "neighbor"}
    expected[pr_key] = "replacement"
    assert _stored(receipt_store) == expected
    assert reviewer_gate_state.get_reviewed_sha(pr_key) == "replacement"
    _writer_available(receipt_store)


@pytest.mark.parametrize("timing", ["BEFORE", "AFTER"])
def test_rejected_review_receipt_preserves_previous_sha(receipt_store, timing):
    with closing(sqlite3.connect(str(receipt_store))) as db:
        db.execute(
            f"CREATE TRIGGER reject_receipt {timing} INSERT ON reviewer_gate_state "
            "BEGIN SELECT RAISE(FAIL, 'review receipt rejected'); END",
        )
        db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="review receipt rejected"):
        reviewer_gate_state.mark_reviewed("fixture/repo#1", "replacement")
    assert not database.get_db().in_transaction
    assert _stored(receipt_store) == {
        "fixture/repo#1": "previous", "fixture/repo#2": "neighbor",
    }
    _writer_available(receipt_store)


def test_review_receipt_commit_failure_preserves_previous_sha(receipt_store, monkeypatch):
    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("review receipt commit failed")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(receipt_store), factory=FailedCommit)
    monkeypatch.setattr(database, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="review receipt commit failed"):
            reviewer_gate_state.mark_reviewed("fixture/repo#1", "replacement")
        assert db.closed
        assert _stored(receipt_store) == {
            "fixture/repo#1": "previous", "fixture/repo#2": "neighbor",
        }
        _writer_available(receipt_store)
    finally:
        db.close()
