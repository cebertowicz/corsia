"""Canonical result writes, echo binding and quarantine have explicit owners."""

import json
import sqlite3
from contextlib import closing

import pytest

from core import thread_history, tool_result_binding
from core.tool_result_delivery import persist_tool_execution_result
from core.turn_core import TurnIdentity
from infra import database
from infra.queries import insert_message
from subagents import harness_tool_bridge


@pytest.fixture
def result_store(isolated_db):
    with database.write_transaction(None, scope="seed_tool_binding_test") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES ('result-thread','fixture-user','original','claude-code/sonnet','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('result-turn','result-thread',0,'claude-code/sonnet','running','t','fixture-boot')",
        )
        insert_message(
            "result-thread", "assistant", "",
            {
                "turn_id": "result-turn", "step_type": "tool_calls",
                "llm_tool_calls": [{
                    "id": "fixture-call", "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"fixture.txt"}',
                    },
                }],
            },
            msg_id="result-step", db=db,
        )
    return isolated_db


def _seed_result():
    with database.write_transaction(None, scope="seed_provisional_test") as db:
        return persist_tool_execution_result(
            thread_id="result-thread", turn_id="result-turn",
            invocation_id="fixture-invocation", assistant_step_id="result-step",
            name="read_file", arguments={"path": "fixture.txt"},
            content="canonical bytes", is_error=False, db=db,
        )


def _act(unit, *, db=None):
    if unit == "bridge":
        assert db is None
        return harness_tool_bridge._persist_delivery(
            thread_id="result-thread", turn_identity=TurnIdentity("result-turn", 0),
            invocation_id="fixture-invocation", provider_tool_call_id="fixture-call",
            assistant_step_id="result-step", name="read_file",
            arguments={"path": "fixture.txt"}, content="canonical bytes",
        )
    if unit == "bind":
        return tool_result_binding.bind_tool_result_echo(
            thread_id="result-thread", provider_tool_call_id="fixture-call",
            assistant_step_id="result-step",
            echoed_content=(
                'canonical bytes\n\n[CORSIA_TOOL_INVOCATION '
                'reference={"invocation_id":"fixture-invocation"}]'
            ),
            db=db,
        )
    assert unit == "quarantine" and db is None
    return thread_history.repair_stranded_tool_lineage("result-thread")


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        thread = db.execute(
            "SELECT title,next_message_seq FROM threads WHERE id='result-thread'",
        ).fetchone()
        messages = db.execute(
            "SELECT id,content,metadata,seq FROM messages "
            "WHERE thread_id='result-thread' ORDER BY seq",
        ).fetchall()
        return thread, messages


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.03)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture(params=["bridge", "bind", "quarantine"])
def unit_store(request, result_store):
    if request.param != "bridge":
        _seed_result()
    if request.param == "quarantine":
        with database.write_transaction(None, scope="seed_crashed_turn_test") as db:
            db.execute("UPDATE active_turns SET status='error' WHERE id='result-turn'")
    return request.param, result_store


def test_implicit_result_writer_preserves_foreign_transaction(unit_store):
    unit, path = unit_store
    before = _stored(path)
    db = database.get_db()
    db.execute("UPDATE threads SET title='caller' WHERE id='result-thread'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _act(unit)
        assert db.in_transaction
        assert _stored(path) == before
        db.commit()
        thread, messages = _stored(path)
        assert thread == ("caller", before[0][1])
        assert messages == before[1]
    finally:
        db.rollback()
        db.close()


def test_clean_named_echo_commits_without_closing_caller(result_store):
    original = _seed_result()
    with closing(sqlite3.connect(str(result_store))) as db:
        db.row_factory = sqlite3.Row
        result = _act("bind", db=db)
        assert result.message_id == original.message_id
        assert result.content == "canonical bytes"
        assert result.provider_tool_call_id == "fixture-call"
        assert not db.in_transaction
        assert db.execute("SELECT 1").fetchone()[0] == 1
        metadata = json.loads(_stored(result_store)[1][-1][2])
        assert metadata["tool_result_binding_state"] == "bound"
        assert metadata["tool_call_id"] == "fixture-call"
        _writer_available(result_store)


def test_outer_rollback_owns_echo_binding(result_store):
    _seed_result()
    before = _stored(result_store)
    with pytest.raises(ValueError, match="abort outer"):
        with database.write_transaction(None, scope="outer_echo_test") as db:
            _act("bind", db=db)
            raise ValueError("abort outer")
    assert _stored(result_store) == before


def test_failed_echo_cannot_leak_into_caller_commit(result_store):
    _seed_result()
    before = _stored(result_store)
    with closing(sqlite3.connect(str(result_store))) as db:
        db.execute(
            "CREATE TRIGGER reject_echo AFTER UPDATE OF metadata ON messages "
            "WHEN NEW.role='tool' BEGIN SELECT RAISE(FAIL, 'echo rejected'); END",
        )
        db.commit()
    with database.write_transaction(None, scope="outer_echo_test") as db:
        db.execute("UPDATE threads SET title='caller' WHERE id='result-thread'")
        with pytest.raises(sqlite3.IntegrityError, match="echo rejected"):
            _act("bind", db=db)
        assert db.in_transaction
    thread, messages = _stored(result_store)
    assert thread == ("caller", before[0][1])
    assert messages == before[1]


def test_result_identity_reads_share_the_write_boundary(unit_store, monkeypatch):
    unit, path = unit_store
    protected_reads = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().startswith("SELECT") and (
                "FROM messages" in sql or "FROM active_turns" in sql
            ):
                try:
                    _writer_available(path)
                except sqlite3.OperationalError as error:
                    assert "locked" in str(error)
                    protected_reads.append(True)
                else:
                    protected_reads.append(False)
            return super().execute(sql, *args, **kwargs)

    db = sqlite3.connect(str(path), factory=ObservedConnection)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    try:
        result = _act(unit)
        assert protected_reads and all(protected_reads)
        if unit == "quarantine":
            assert result == 1
            assert json.loads(_stored(path)[1][-1][2])[
                "tool_result_binding_state"
            ] == "quarantined_unbound"
        else:
            assert result.content == "canonical bytes"
            assert result.assistant_step_id == "result-step"
            assert result.provider_tool_call_id == "fixture-call"
        _writer_available(path)
    finally:
        db.close()


def test_result_commit_failure_preserves_prior_rows(unit_store, monkeypatch):
    unit, path = unit_store
    before = _stored(path)

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("result commit rejected")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(path), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="result commit rejected"):
            _act(unit)
        assert db.closed
        assert _stored(path) == before
        _writer_available(path)
    finally:
        db.close()


def test_echo_lineage_lookups_use_partial_index(result_store):
    original = _seed_result()
    with closing(sqlite3.connect(str(result_store))) as db:
        db.row_factory = sqlite3.Row
        observed = []
        db.set_trace_callback(observed.append)
        bound = _act("bind", db=db)
        reread = tool_result_binding.read_bound_tool_result(
            thread_id="result-thread", provider_tool_call_id="fixture-call", db=db,
        )
        db.set_trace_callback(None)

        assert bound.message_id == original.message_id
        assert bound.provider_tool_call_id == "fixture-call"
        assert reread is not None
        assert reread.message_id == original.message_id
        lookups = [
            statement for statement in observed
            if statement.startswith("SELECT")
            and "FROM messages" in statement
            and "role='tool'" in statement
            and "json_extract(metadata" in statement
        ]
        assert len(lookups) == 3
        assert sum("$.assistant_step_id" in statement for statement in lookups) == 2
        assert sum("$.tool_call_id" in statement for statement in lookups) == 2
        for statement in lookups:
            plan = db.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
            assert any("idx_messages_tool_lineage" in str(row[-1]) for row in plan)
