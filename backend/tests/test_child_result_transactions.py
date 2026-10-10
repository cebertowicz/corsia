"""Replay identity and tool declarations share their caller's transaction."""

import sqlite3
from contextlib import closing

import pytest

from infra import database
from subagents import child_thread


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        thread = db.execute(
            "SELECT title,next_message_seq FROM threads WHERE id='child-thread'",
        ).fetchone()
        rows = db.execute(
            "SELECT id,content,metadata,seq FROM messages ORDER BY seq",
        ).fetchall()
        return thread, rows


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.03)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture(params=["replay", "new_step", "append", "result"])
def writer_case(request, isolated_db):
    with database.write_transaction(None, scope="seed_child_result_test") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES ('child-thread','fixture-user','original','claude-code/sonnet','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('child-turn','child-thread',0,'claude-code/sonnet','running','t','fixture-boot')",
        )
    step_id = None
    if request.param in {"append", "result"}:
        step_id = child_thread.persist_tool_use(
            "child-thread", turn_id="child-turn", tool_id="first-call",
            name="read_file", arguments={"path": "fixture.txt"},
            provider_turn_id="provider-turn",
        )
    return request.param, isolated_db, step_id


def _act(case, *, db=None):
    unit, _path, step_id = case
    if unit == "replay":
        return child_thread._insert_message_once(
            "child-thread", "assistant", "replayed", {"turn_id": "child-turn"},
            msg_id="replay-row", legacy_msg_id="legacy-row", db=db,
        )
    if unit == "result":
        return child_thread.persist_tool_result(
            "child-thread", turn_id="child-turn", tool_use_id="first-call",
            name="read_file", content="canonical result", assistant_step_id=step_id,
            db=db,
        )
    return child_thread.persist_tool_use(
        "child-thread", turn_id="child-turn", tool_id="next-call",
        name="read_file", arguments={"path": "next.txt"},
        provider_turn_id="provider-turn", db=db,
    )


def test_child_writer_refuses_foreign_transaction(writer_case):
    _unit, path, _step = writer_case
    before = _stored(path)
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='child-thread'")
    try:
        with pytest.raises(RuntimeError):
            _act(writer_case)
        assert db.in_transaction
        assert _stored(path) == before
        db.commit()
        thread, rows = _stored(path)
        assert thread == ("caller", before[0][1])
        assert rows == before[1]
    finally:
        db.rollback()
        db.close()


def test_child_identity_lookup_excludes_competing_writer(writer_case, monkeypatch):
    unit, path, step_id = writer_case
    protected = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().startswith("SELECT") and (
                "FROM messages" in sql or "FROM active_turns" in sql
            ):
                try:
                    _writer_available(path)
                except sqlite3.OperationalError as error:
                    assert "locked" in str(error)
                    protected.append(True)
                else:
                    protected.append(False)
            return super().execute(sql, *args, **kwargs)

    db = sqlite3.connect(str(path), factory=ObservedConnection)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(child_thread, "get_db", lambda: db)
    try:
        result_id = _act(writer_case)
        assert protected and all(protected)
        rows = _stored(path)[1]
        assert result_id in {row[0] for row in rows}
        if unit == "append":
            assert result_id == step_id and len(rows) == 1
        _writer_available(path)
    finally:
        db.close()


def test_child_outer_rollback_owns_all_row_changes(writer_case):
    _unit, path, _step = writer_case
    before = _stored(path)
    with pytest.raises(ValueError, match="abort caller"):
        with database.write_transaction(None, scope="outer_child_result_test") as db:
            db.execute("UPDATE threads SET title='caller' WHERE id='child-thread'")
            _act(writer_case, db=db)
            raise ValueError("abort caller")
    assert _stored(path) == before
    _writer_available(path)


def test_child_final_commit_failure_preserves_prior_rows(writer_case, monkeypatch):
    _unit, path, _step = writer_case
    before = _stored(path)

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("child commit rejected")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(path), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(child_thread, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="child commit rejected"):
            _act(writer_case)
        assert db.closed
        assert _stored(path) == before
        _writer_available(path)
    finally:
        db.close()
