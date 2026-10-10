"""Comment mutations conserve their declared caller's transaction and data."""

from contextlib import closing
import sqlite3

import pytest

from doceditor import comments


BASE_ROWS = [
    ("first", "original", 0, 6, 0, 0),
    ("foreign", "original", 0, 6, 0, 0),
    ("second", "original", 0, 6, 0, 0),
]


def _rows(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT id,body,from_off,to_off,resolved,acked FROM doc_comments ORDER BY id"
        ).fetchall()


@pytest.fixture
def stored_comments(isolated_db):
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.executemany(
            "INSERT INTO doc_comments "
            "(id,artifact_id,author,body,from_off,to_off,created_at,updated_at) "
            "VALUES (?,?,'test-author','original',0,6,'t','t')",
            [("first", "document"), ("second", "document"), ("foreign", "other")],
        )
        db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('comment-outer','original','t')")
        db.commit()
    return isolated_db


def _change(operation, *, db=None):
    if operation == "create":
        return comments.create("document", body="new", client_id="new", db=db)
    if operation == "update":
        return comments.update("first", {"body": "changed", "resolved": True, "acked": True}, db=db)
    return comments.update_positions("document", [
        {"id": "first", "from_off": 10, "to_off": 16},
        {"id": "second", "from_off": 20, "to_off": 26},
        {"id": "foreign", "from_off": 200, "to_off": 206},
    ], db=db)


@pytest.mark.parametrize("operation", ["create", "update", "positions"])
def test_implicit_comment_owner_cannot_discard_or_commit_outer_work(stored_comments, operation):
    from infra.database import TransactionOwnershipError, get_db

    db = get_db()
    db.execute("UPDATE settings SET value='outer' WHERE key='comment-outer'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _change(operation)
        assert db.in_transaction
        assert _rows(stored_comments) == BASE_ROWS
        db.commit()
        with closing(sqlite3.connect(str(stored_comments))) as reader:
            assert reader.execute("SELECT value FROM settings WHERE key='comment-outer'").fetchone() == ("outer",)
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("operation", ["create", "update", "positions"])
def test_caller_rollback_reverts_comment_write(stored_comments, operation):
    from infra.database import write_transaction

    with pytest.raises(ValueError, match="abort outer"):
        with write_transaction(None, scope="comment_test_owner") as db:
            _change(operation, db=db)
            raise ValueError("abort outer")
    assert _rows(stored_comments) == BASE_ROWS


@pytest.mark.parametrize("operation,event,target", [
    ("create", "INSERT", "new"),
    ("update", "UPDATE OF body", "first"),
    ("positions", "UPDATE OF from_off", "second"),
])
def test_partial_comment_failure_rolls_back_only_its_unit(stored_comments, operation, event, target):
    from infra.database import write_transaction

    with closing(sqlite3.connect(str(stored_comments))) as db:
        db.execute(
            f"CREATE TRIGGER reject_comment AFTER {event} ON doc_comments "
            f"WHEN NEW.id='{target}' BEGIN SELECT RAISE(FAIL, 'comment rejected'); END"
        )
        db.commit()
    with write_transaction(None, scope="comment_test_owner") as db:
        db.execute("UPDATE settings SET value='outer' WHERE key='comment-outer'")
        with pytest.raises(sqlite3.IntegrityError, match="comment rejected"):
            _change(operation, db=db)
        assert db.in_transaction
    assert _rows(stored_comments) == BASE_ROWS
    with closing(sqlite3.connect(str(stored_comments))) as reader:
        assert reader.execute("SELECT value FROM settings WHERE key='comment-outer'").fetchone() == ("outer",)


@pytest.mark.parametrize("operation", ["create", "update", "positions"])
def test_comment_commit_failure_releases_the_owned_writer(stored_comments, monkeypatch, operation):
    from infra import database

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("comment commit failed")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(stored_comments), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(comments, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="comment commit failed"):
            _change(operation)
        assert db.closed
        assert _rows(stored_comments) == BASE_ROWS
        with closing(sqlite3.connect(str(stored_comments), timeout=0.05)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
    finally:
        db.close()


def test_comment_result_read_failure_does_not_commit_the_patch(stored_comments):
    db = sqlite3.connect(str(stored_comments))
    db.row_factory = sqlite3.Row
    db.set_authorizer(lambda action, table, column, *_:
                      sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                      and table == "doc_comments" and column == "body" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            comments.update("first", {"body": "changed"}, db=db)
        assert not db.in_transaction
        assert _rows(stored_comments) == BASE_ROWS
        assert db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["create", "update", "positions"])
def test_comment_result_is_durable_and_artifact_scoped(stored_comments, operation):
    result = _change(operation)
    if operation == "create":
        assert (result["id"], result["artifact_id"], result["body"]) == ("new", "document", "new")
        assert _rows(stored_comments) == [*BASE_ROWS[:2], ("new", "new", None, None, 0, 0), BASE_ROWS[2]]
    elif operation == "update":
        assert (result["body"], result["resolved"], result["acked"]) == ("changed", 1, 1)
        assert _rows(stored_comments) == [("first", "changed", 0, 6, 1, 1), *BASE_ROWS[1:]]
    else:
        assert result == 2
        assert _rows(stored_comments) == [
            ("first", "original", 10, 16, 0, 0), BASE_ROWS[1], ("second", "original", 20, 26, 0, 0),
        ]


def test_comment_empty_unknown_and_missing_updates_preserve_caller_work(stored_comments):
    from infra.database import write_transaction

    with write_transaction(None, scope="comment_test_owner") as db:
        db.execute("UPDATE settings SET value='outer' WHERE key='comment-outer'")
        assert comments.update("first", {"artifact_id": "other"}, db=db) is None
        assert comments.update("missing", {"body": "ignored"}, db=db) is None
        assert comments.update_positions("document", [], db=db) == 0
        assert db.in_transaction
    assert _rows(stored_comments) == BASE_ROWS
