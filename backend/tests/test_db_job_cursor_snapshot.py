"""A cursor left open by one DB job never pins the pool thread's read snapshot."""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import tool_result_binding
from core.turn_core import TurnIdentity
from infra import database, db_async
from infra.queries import insert_message
from subagents import harness_tool_bridge

# Two rows, so a cursor that has yielded the first is still mid-statement.
_UNFINISHED_READ = (
    "SELECT m.id FROM messages m, json_each('[1,2]') WHERE m.thread_id='result-thread'"
)


class _Abandoned(Exception):
    """A job failure raised while its cursor was still iterating."""


@pytest.fixture
def result_store(isolated_db):
    with database.write_transaction(None, scope="seed_cursor_snapshot_test") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES ('result-thread','fixture-user','original','codex/gpt-5.6-terra','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('result-turn','result-thread',0,'codex/gpt-5.6-terra','running','t','fixture-boot')",
        )
        insert_message(
            "result-thread", "assistant", "",
            {
                "turn_id": "result-turn", "step_type": "tool_calls",
                "llm_tool_calls": [{
                    "id": "fixture-call", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"fixture.txt"}'},
                }],
            },
            msg_id="result-step", db=db,
        )
    return isolated_db


def _job(pool, fn, scope):
    return pool.submit(db_async._run_db_job, fn, scope)


def _persist_bound_result():
    return harness_tool_bridge._persist_delivery(
        thread_id="result-thread", turn_identity=TurnIdentity("result-turn", 0),
        invocation_id="fixture-invocation", provider_tool_call_id="fixture-call",
        assistant_step_id="result-step", name="read_file",
        arguments={"path": "fixture.txt"}, content="canonical bytes",
    )


def _touch_thread():
    with database.write_transaction(None, scope="after_abandoned_cursor") as db:
        db.execute("UPDATE threads SET updated_at='u' WHERE id='result-thread'")


def _abandon_mid_iteration():
    cursor = database.get_db().execute(_UNFINISHED_READ)
    for row in cursor:
        raise _Abandoned(row["id"])


def _echo():
    return tool_result_binding.read_bound_tool_result(
        thread_id="result-thread", provider_tool_call_id="fixture-call",
    )


def test_raised_job_cursor_cannot_hide_a_committed_result_from_the_next_job(result_store):
    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        # The awaiting caller holds the job's exception, and with it the
        # job's frame and cursor, while the pool thread runs other jobs.
        retained = _job(pool, _abandon_mid_iteration, "abandon").exception()
        assert isinstance(retained, _Abandoned)

        _persist_bound_result()

        echoed = _job(pool, _echo, "provider_echo").result()
        assert echoed is not None
        assert echoed.provider_tool_call_id == "fixture-call"
        _job(pool, _touch_thread, "write_after").result()
        del retained


def test_raised_job_cursor_cannot_refuse_the_next_jobs_result_write(result_store):
    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        retained = _job(pool, _abandon_mid_iteration, "abandon").exception()
        assert isinstance(retained, _Abandoned)
        _touch_thread()  # another connection commits after the pinned snapshot

        # The production result save: _persist_delivery_off_loop runs this job.
        try:
            stored = _job(pool, _persist_bound_result, "persist_harness_tool_result").result()
        except sqlite3.OperationalError as error:
            pytest.fail(f"pool thread refused the result write: {error.sqlite_errorname}")
        echoed = _job(pool, _echo, "provider_echo").result()
        del retained

    assert stored.provider_tool_call_id == "fixture-call"
    assert stored.content == "canonical bytes"
    assert echoed == stored


def test_caller_owned_inline_savepoint_keeps_its_cursors(result_store):
    with database.write_transaction(None, scope="caller_owned") as db:
        held = db.execute(_UNFINISHED_READ)
        assert held.fetchone() is not None

        def nested():
            with database.write_transaction(db, scope="nested_savepoint") as savepoint:
                savepoint.execute("UPDATE threads SET title='nested' WHERE id='result-thread'")
                return savepoint.execute(_UNFINISHED_READ)

        # The caller owns the connection, so the job runs inline, outside any DB job.
        inline = db_async.run_in_db_unless_owned(db, nested)
        with pytest.raises(StopIteration) as finished:
            inline.send(None)
        cursor = finished.value.value
        assert cursor.fetchone() is not None
        assert held.fetchone() is not None
        cursor.close()
        held.close()

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        title = _job(pool, lambda: database.get_db().execute(
            "SELECT title FROM threads WHERE id='result-thread'",
        ).fetchone()[0], "read_title").result()
    assert title == "nested"


def test_cursor_escaping_a_returned_job_is_closed_and_named_without_sql(result_store, caplog):
    caplog.set_level(logging.ERROR, logger="infra.db_connection")
    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        escaped = _job(
            pool, lambda: database.get_db().execute(_UNFINISHED_READ), "escape",
        ).result()

    with pytest.raises(sqlite3.ProgrammingError):
        escaped.fetchone()
    [record] = [r for r in caplog.records if r.name == "infra.db_connection"]
    message = record.getMessage()
    assert "scope=escape" in message
    assert "tests/test_db_job_cursor_snapshot.py:" in message
    assert "SELECT" not in message and "result-thread" not in message


def test_job_closes_only_its_own_pooled_cursors(result_store, caplog):
    from infra.db_connection import job_cursors

    caplog.set_level(logging.WARNING, logger="infra.db_connection")
    unowned = database.get_db().execute(_UNFINISHED_READ)
    assert unowned.fetchone() is not None

    def outer_job():
        private = sqlite3.connect(str(result_store), check_same_thread=False)
        private_cursor = private.execute("SELECT id FROM messages")
        pooled = database.get_db()
        outer = pooled.execute(_UNFINISHED_READ)
        assert outer.fetchone() is not None
        with job_cursors("inner"):
            inner = pooled.execute(_UNFINISHED_READ)
            assert inner.fetchone() is not None
        with pytest.raises(sqlite3.ProgrammingError):
            inner.fetchone()
        assert outer.fetchone() is not None
        outer.close()
        return private, private_cursor

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        private, private_cursor = _job(pool, outer_job, "outer").result()

    assert private_cursor.fetchone() is not None
    private.close()
    assert unowned.fetchone() is not None
    unowned.close()
    scopes = [r.getMessage().split("scope=")[1].split()[0] for r in caplog.records
              if r.name == "infra.db_connection"]
    assert scopes == ["inner"]


def test_raised_job_closes_its_query_cursor_with_a_warning(result_store, caplog):
    caplog.set_level(logging.WARNING, logger="infra.db_connection")

    def abandon():
        cursor = database.get_db().execute(_UNFINISHED_READ)
        assert cursor.fetchone() is not None
        raise _Abandoned("mid-iteration")

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        retained = _job(pool, abandon, "abandon").exception()

    assert isinstance(retained, _Abandoned)
    [record] = [r for r in caplog.records if r.name == "infra.db_connection"]
    assert record.levelno == logging.WARNING
    assert "scope=abandon" in record.getMessage()
    assert "error=_Abandoned" in record.getMessage()


def test_raised_job_with_a_fully_fetched_cursor_does_not_warn(result_store, caplog):
    caplog.set_level(logging.WARNING, logger="infra.db_connection")
    captured = []

    def abandon_after_fetching():
        cursor = database.get_db().execute(_UNFINISHED_READ)
        assert list(cursor)
        captured.append(cursor)
        raise _Abandoned("after-fetch")

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        retained = _job(pool, abandon_after_fetching, "fully_fetched").exception()

    assert isinstance(retained, _Abandoned)
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].fetchone()
    assert [r for r in caplog.records if r.name == "infra.db_connection"] == []


def test_returned_job_with_a_completed_insert_cursor_does_not_error(result_store, caplog):
    caplog.set_level(logging.ERROR, logger="infra.db_connection")

    def completed_insert():
        with database.write_transaction(None, scope="completed_insert") as db:
            cursor = db.execute(
                "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
                "VALUES ('inserted-thread','fixture-user','inserted','codex/gpt-5.6-terra','t','t')",
            )
            assert cursor.lastrowid is not None
        return cursor

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        escaped = _job(pool, completed_insert, "completed_insert").result()

    with pytest.raises(sqlite3.ProgrammingError):
        escaped.fetchone()
    assert [r for r in caplog.records if r.name == "infra.db_connection"] == []


def test_raised_job_with_an_unexhausted_select_warns_and_closes(result_store, caplog):
    caplog.set_level(logging.WARNING, logger="infra.db_connection")
    captured = []

    def abandon_before_exhaustion():
        cursor = database.get_db().execute(_UNFINISHED_READ)
        assert cursor.fetchone() is not None
        captured.append(cursor)
        raise _Abandoned("before-exhaustion")

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        retained = _job(pool, abandon_before_exhaustion, "unexhausted_raised").exception()

    assert isinstance(retained, _Abandoned)
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].fetchone()
    [record] = [r for r in caplog.records if r.name == "infra.db_connection"]
    assert record.levelno == logging.WARNING
    assert "scope=unexhausted_raised" in record.getMessage()
    assert "error=_Abandoned" in record.getMessage()


def test_returned_job_with_an_unexhausted_select_errors_and_closes(result_store, caplog):
    caplog.set_level(logging.ERROR, logger="infra.db_connection")

    def return_before_exhaustion():
        cursor = database.get_db().execute(_UNFINISHED_READ)
        assert cursor.fetchone() is not None
        return cursor

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        escaped = _job(pool, return_before_exhaustion, "unexhausted_returned").result()

    with pytest.raises(sqlite3.ProgrammingError):
        escaped.fetchone()
    [record] = [r for r in caplog.records if r.name == "infra.db_connection"]
    assert record.levelno == logging.ERROR
    assert "scope=unexhausted_returned" in record.getMessage()
    assert "query=True" in record.getMessage()


@pytest.mark.parametrize(("label", "sql", "fetch"), [
    ("single_row_lookup", "SELECT id FROM threads WHERE id='result-thread'", True),
    ("empty_result", "SELECT id FROM threads WHERE id='no-such-thread'", False),
])
def test_returned_job_with_a_finished_select_does_not_error(result_store, caplog, label, sql, fetch):
    caplog.set_level(logging.WARNING, logger="infra.db_connection")

    def keep_finished_cursor():
        cursor = database.get_db().execute(sql)
        if fetch:
            assert cursor.fetchone() is not None
        return cursor

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        escaped = _job(pool, keep_finished_cursor, label).result()

    with pytest.raises(sqlite3.ProgrammingError):
        escaped.fetchone()
    assert [r for r in caplog.records if r.name == "infra.db_connection"] == []


def test_returned_job_names_the_error_its_escaped_statement_hits(result_store, caplog):
    caplog.set_level(logging.ERROR, logger="infra.db_connection")

    def return_before_a_failing_row():
        # sqlite3 steps one row ahead: the first fetch reads row 1 and steps to
        # row 2, so only the probe at job exit steps into the malformed row 3.
        cursor = database.get_db().execute(
            "SELECT json(column1) FROM (VALUES ('1'), ('2'), ('{malformed'))",
        )
        assert cursor.fetchone() is not None
        return cursor

    with ThreadPoolExecutor(1, thread_name_prefix="db-fixture") as pool:
        escaped = _job(pool, return_before_a_failing_row, "failing_step").result()

    with pytest.raises(sqlite3.ProgrammingError):
        escaped.fetchone()
    [record] = [r for r in caplog.records if r.name == "infra.db_connection"]
    assert record.levelno == logging.ERROR
    assert "scope=failing_step" in record.getMessage()
    assert "step_error=OperationalError" in record.getMessage()
