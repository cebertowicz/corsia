"""Real-store receipts preserve one writer and the exact transcript target."""

import json
import sqlite3
from contextlib import closing

import pytest

from infra import context_receipts, database
from infra.queries import insert_message


@pytest.fixture(params=["native", "claude-code", "codex"])
def identity(request):
    harness = request.param
    return context_receipts.ContextReceiptIdentity(
        harness, f"{harness}/fixture", 7,
        None if harness == "native" else "a" * 64,
    )


@pytest.fixture
def receipt_store(isolated_db):
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.row_factory = sqlite3.Row
        for thread_id in ("receipt-thread", "other-thread"):
            db.execute(
                "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
                "VALUES (?,'test-user','original','t','t')", (thread_id,),
            )
        for message_id, thread_id, role in (
            ("assistant-1", "receipt-thread", "assistant"),
            ("assistant-2", "receipt-thread", "assistant"),
            ("user-3", "receipt-thread", "user"),
            ("other-4", "other-thread", "assistant"),
        ):
            insert_message(
                thread_id, role, "unchanged content",
                {"turn_id": "receipt-turn", "reasoning": "keep"},
                msg_id=message_id, created_at="2026-09-13T00:00:00Z", db=db,
            )
        db.commit()
    return isolated_db


def _attach(identity, *, db=None):
    return context_receipts.attach_context_receipt(
        "receipt-thread", "assistant-2",
        context_usage={"total_used": 120, "token_source": "measured"},
        identity=identity, db=db,
    )


def _stored(path, message_id="assistant-2"):
    with closing(sqlite3.connect(str(path))) as db:
        metadata = db.execute(
            "SELECT metadata FROM messages WHERE id=?", (message_id,),
        ).fetchone()[0]
        title = db.execute(
            "SELECT title FROM threads WHERE id='receipt-thread'",
        ).fetchone()[0]
    return json.loads(metadata), title


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.03)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def test_implicit_receipt_refuses_foreign_pooled_transaction(receipt_store, identity):
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='receipt-thread'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _attach(identity)
        assert db.in_transaction
        assert _stored(receipt_store) == ({"turn_id": "receipt-turn", "reasoning": "keep"}, "original")
        db.commit()
        assert _stored(receipt_store) == ({"turn_id": "receipt-turn", "reasoning": "keep"}, "caller")
    finally:
        db.rollback()
        db.close()


def test_explicit_clean_receipt_commits_without_closing_caller(receipt_store, identity):
    with closing(sqlite3.connect(str(receipt_store))) as db:
        db.row_factory = sqlite3.Row
        receipt = _attach(identity, db=db)
        assert not db.in_transaction
        assert db.execute("SELECT 1").fetchone()[0] == 1
        metadata, title = _stored(receipt_store)
        assert metadata["context_usage"] == receipt
        assert metadata["reasoning"] == "keep"
        assert title == "original"
        assert receipt["total_used"] == 120
        assert receipt["harness"] == identity.harness
        assert receipt["runtime_context_epoch"] == 7
        assert receipt["catalog_model_id"] == f"{identity.harness}/fixture"


def test_outer_rollback_owns_receipt(receipt_store, identity):
    with pytest.raises(ValueError, match="abort outer"):
        with database.write_transaction(None, scope="receipt_outer_test") as db:
            assert _attach(identity, db=db) is not None
            raise ValueError("abort outer")
    assert _stored(receipt_store) == ({"turn_id": "receipt-turn", "reasoning": "keep"}, "original")


def test_failed_receipt_cannot_leak_into_outer_commit(receipt_store, identity):
    with closing(sqlite3.connect(str(receipt_store))) as db:
        db.execute(
            "CREATE TRIGGER reject_receipt AFTER UPDATE OF metadata ON messages "
            "BEGIN SELECT RAISE(FAIL, 'receipt rejected'); END"
        )
        db.commit()
    with database.write_transaction(None, scope="receipt_outer_test") as db:
        db.execute("UPDATE threads SET title='caller' WHERE id='receipt-thread'")
        with pytest.raises(sqlite3.IntegrityError, match="receipt rejected"):
            _attach(identity, db=db)
        assert db.in_transaction
    assert _stored(receipt_store) == ({"turn_id": "receipt-turn", "reasoning": "keep"}, "caller")


@pytest.mark.parametrize("explicit", [False, True])
def test_receipt_commit_failure_releases_writer(receipt_store, identity, monkeypatch, explicit):
    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("receipt commit failed")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(receipt_store), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    # Exercise the direct writer factory, not cross-job pooled cleanup.
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(context_receipts, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="receipt commit failed"):
            _attach(identity, db=db if explicit else None)
        assert db.closed == (not explicit)
        if explicit:
            assert not db.in_transaction
            assert db.execute("SELECT 1").fetchone()[0] == 1
        assert _stored(receipt_store) == ({"turn_id": "receipt-turn", "reasoning": "keep"}, "original")
        _writer_available(receipt_store)
    finally:
        db.close()


def test_latest_selection_and_attachment_share_writer(receipt_store, identity, monkeypatch):
    protected_reads = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().startswith("SELECT") and "FROM messages" in sql:
                try:
                    _writer_available(receipt_store)
                except sqlite3.OperationalError as error:
                    assert "locked" in str(error)
                    protected_reads.append(True)
                else:
                    protected_reads.append(False)
            return super().execute(sql, *args, **kwargs)

    db = sqlite3.connect(str(receipt_store), factory=ObservedConnection)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(context_receipts, "get_db", lambda: db)
    try:
        result = context_receipts.attach_delivered_context_to_latest_assistant(
            thread_id="receipt-thread",
            step_ids=["assistant-1", "assistant-2", "user-3", "other-4"],
            context_usage={"total_used": 120}, identity=identity,
        )
        assert result is not None
        assert result[0] == "assistant-2"
        assert protected_reads == [True, True]
        assert _stored(receipt_store)[0]["context_usage"] == result[1]
        for message_id in ("assistant-1", "user-3", "other-4"):
            assert "context_usage" not in _stored(receipt_store, message_id)[0]
        _writer_available(receipt_store)
    finally:
        db.close()


@pytest.mark.parametrize("step_ids", [[], ["missing"], ["user-3", "other-4"]])
def test_no_eligible_assistant_keeps_transcript_unchanged(receipt_store, identity, step_ids):
    assert context_receipts.attach_delivered_context_to_latest_assistant(
        thread_id="receipt-thread", step_ids=step_ids,
        context_usage={"total_used": 120}, identity=identity,
    ) is None
    assert _stored(receipt_store) == ({"turn_id": "receipt-turn", "reasoning": "keep"}, "original")
    _writer_available(receipt_store)
