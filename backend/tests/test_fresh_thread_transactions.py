"""Thread creation owns its DB unit; visibility follows durable state."""

import sqlite3
from contextlib import closing

import pytest

from infra import database, fresh_threads, queries
from subagents import child_thread
from tests.event_routing_helpers import mk_session


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        threads = db.execute(
            "SELECT id,title,spawned_by,next_message_seq,last_context_change_seq "
            "FROM threads ORDER BY id",
        ).fetchall()
        messages = db.execute(
            "SELECT thread_id,role,content,metadata,seq FROM messages ORDER BY seq",
        ).fetchall()
        return threads, messages


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.02)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _create(kind, *, db=None):
    kwargs = dict(user_id="user-1", thread_id="child", title="Child", model=None, db=db)
    if kind == "fresh":
        return fresh_threads.create_fresh_thread(**kwargs)
    return child_thread.create_child_thread(parent_thread_id="parent", **kwargs)


@pytest.fixture(params=["fresh", "child"])
def creation_case(request, isolated_db):
    mk_session("parent")
    return isolated_db, request.param


def test_creation_keeps_named_caller_rollback_authority(creation_case):
    path, kind = creation_case
    before = _stored(path)
    with pytest.raises(RuntimeError, match="caller abort"):
        with database.write_transaction(None, scope="create_thread_caller") as db:
            db.execute("UPDATE threads SET title='caller' WHERE id='parent'")
            _create(kind, db=db)
            assert db.in_transaction
            assert _stored(path) == before
            raise RuntimeError("caller abort")
    assert _stored(path) == before
    _writer_available(path)


def test_creation_refuses_a_foreign_anonymous_owner(creation_case):
    path, kind = creation_case
    before = _stored(path)
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='parent'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _create(kind)
        assert db.in_transaction
        assert _stored(path) == before
        db.commit()
        assert _stored(path) == ([("parent", "caller", None, 1, 0)], [])
    finally:
        db.rollback()
        db.close()


def test_failed_creation_sql_leaves_no_pooled_write(creation_case):
    path, kind = creation_case
    before = _stored(path)
    with database.write_transaction(None, scope="create_thread_fault_seed") as db:
        db.execute(
            "CREATE TRIGGER reject_child AFTER INSERT ON threads "
            "WHEN NEW.id='child' BEGIN SELECT RAISE(FAIL, 'child rejected'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="child rejected"):
        _create(kind)
    assert not database.get_db().in_transaction
    assert _stored(path) == before
    _writer_available(path)


def test_creation_commit_failure_preserves_named_connection(creation_case):
    path, kind = creation_case
    before = _stored(path)

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("creation commit rejected")

    with closing(sqlite3.connect(str(path), factory=FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError, match="creation commit rejected"):
            _create(kind, db=db)
        assert not db.in_transaction
        assert db.execute("SELECT 1").fetchone()[0] == 1
        assert _stored(path) == before
        _writer_available(path)


@pytest.mark.asyncio
async def test_visible_creation_checks_parent_inside_writer_and_publishes_after_commit(
    isolated_db, monkeypatch,
):
    mk_session("parent")
    protected, published = [], []
    real_make_connection = database._make_connection

    def observe(sql):
        if sql.startswith("SELECT id FROM threads WHERE id =") and "user_id" in sql:
            try:
                _writer_available(isolated_db)
            except sqlite3.OperationalError as error:
                protected.append("locked" in str(error))
            else:
                protected.append(False)

    def factory(path):
        db = real_make_connection(path)
        db.set_trace_callback(observe)
        return db

    def broadcast(user_id, payload):
        _writer_available(isolated_db)
        assert any(row[0] == "child" for row in _stored(isolated_db)[0])
        published.append((user_id, payload))

    monkeypatch.setattr(database, "_make_connection", factory)
    monkeypatch.setattr("infra.state.broadcast_to_user", broadcast)
    session = await fresh_threads.create_visible_fresh_thread(
        user_id="user-1", thread_id="child", title="Child", spawned_by="parent",
    )
    assert protected and all(protected)
    assert session["spawned_by"] == "parent"
    assert published == [("user-1", {"type": "session_created", "session": session})]


@pytest.mark.asyncio
async def test_failed_spawn_marker_rolls_back_message_but_keeps_created_child(
    isolated_db, monkeypatch,
):
    mk_session("parent")
    published = []

    def fail_after_message(*args, **kwargs):
        queries.insert_message(*args, **kwargs)
        raise RuntimeError("marker rejected after insertion")

    monkeypatch.setattr(fresh_threads, "insert_message", fail_after_message)
    monkeypatch.setattr("infra.state.broadcast_to_user", lambda *args: None)
    monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *args: published.append(args))
    result = await fresh_threads.create_visible_fresh_thread(
        user_id="user-1", thread_id="child", title="Child", spawned_by="parent",
        emit_spawn_chip=True,
    )
    assert result["id"] == "child"
    assert _stored(isolated_db) == (
        [("child", "Child", "parent", 1, 0), ("parent", "T", None, 1, 0)], [],
    )
    assert published == []
    _writer_available(isolated_db)
