"""Native result durability is independent of its post-commit observer."""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from core import step_handler, tool_result_delivery
from core.turn_core import TurnIdentity
from infra import database
from infra.queries import insert_message


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        thread = db.execute(
            "SELECT title,next_message_seq FROM threads WHERE id='native-thread'",
        ).fetchone()
        rows = db.execute(
            "SELECT id,content,metadata,seq FROM messages ORDER BY seq",
        ).fetchall()
        return thread, rows


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.03)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture
def native_result(isolated_db, monkeypatch):
    with database.write_transaction(None, scope="seed_native_result_test") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES ('native-thread','fixture-user','original','test/model','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('native-turn','native-thread',0,'test/model','running','t','fixture-boot')",
        )
        insert_message(
            "native-thread", "assistant", "",
            {"turn_id": "native-turn", "llm_tool_calls": [{
                "id": "native-call", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"fixture.txt"}'},
            }]},
            msg_id="native-step", db=db,
        )
    frames = []

    async def broadcast(frame):
        frames.append(frame)

    async def run_job(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(step_handler, "run_in_db", run_job)
    handler = step_handler.StepHandler(
        "native-thread", {"model": "test/model"}, broadcast, "native-turn",
    )
    handler.bind_turn_identity(TurnIdentity("native-turn", 0))
    handler.tool_step_id = "native-step"
    handler.tool_calls = [{"id": "native-call", "name": "read_file", "result": None}]
    return isolated_db, handler, frames


def _complete(handler):
    return asyncio.run(handler.handle("tool_complete", {
        "id": "native-call", "name": "read_file", "result": "canonical result",
    }))


def test_native_result_refuses_foreign_transaction(native_result):
    path, handler, frames = native_result
    before = _stored(path)
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='native-thread'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _complete(handler)
        assert db.in_transaction
        assert _stored(path) == before
        assert frames == []
        db.commit()
        thread, rows = _stored(path)
        assert thread == ("caller", before[0][1])
        assert rows == before[1]
    finally:
        db.rollback()
        db.close()


def test_native_post_insert_error_cannot_publish_a_partial_result(native_result, monkeypatch):
    path, handler, frames = native_result
    before = _stored(path)

    def fail_after_insert(*args, **kwargs):
        insert_message(*args, **kwargs)
        raise sqlite3.OperationalError("result insert rejected")

    # The native row is written by the canonical tool-result writer.
    monkeypatch.setattr(tool_result_delivery, "insert_message", fail_after_insert)
    with pytest.raises(sqlite3.OperationalError, match="result insert rejected"):
        _complete(handler)
    assert _stored(path) == before
    assert frames == []
    _writer_available(path)


def test_native_commit_failure_keeps_prior_rows(native_result, monkeypatch):
    path, handler, frames = native_result
    before = _stored(path)

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("native commit rejected")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(path), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(step_handler, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="native commit rejected"):
            _complete(handler)
        assert db.closed
        assert _stored(path) == before
        assert frames == []
        _writer_available(path)
    finally:
        db.close()


@pytest.mark.parametrize("observer_fails", [False, True])
def test_native_observer_runs_after_durable_result(native_result, monkeypatch, observer_fails):
    path, handler, frames = native_result
    observed = []

    def observe(*args, **kwargs):
        row = _stored(path)[1][-1]
        assert row[1] == "canonical result"
        metadata = json.loads(row[2])
        assert metadata["assistant_step_id"] == "native-step"
        assert metadata["tool_call_id"] == "native-call"
        _writer_available(path)
        observed.append(row)
        if observer_fails:
            raise RuntimeError("observer rejected")

    monkeypatch.setattr("registry.observer.observe_record", observe)
    result = _complete(handler)
    assert len(observed) == 1
    assert (result.message_id, result.content, result.seq) == (
        observed[0][0], "canonical result", 2,
    )
    assert len(frames) == 1 and frames[0]["type"] == "tool_done"
    assert frames[0]["result"] == result.content
    assert _stored(path)[1][-1] == observed[0]
