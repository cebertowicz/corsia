"""Terminal-error references and transient retry guidance."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger

from api.thread_paging import finish_display
from infra.turn_errors import (
    _LAST_TURN_ERROR_SQL,
    _LATEST_TURN_SQL,
    apply_error_next_steps,
    insert_turn_error,
    last_turn_error_id,
)
from infra.failure_reference import REFERENCE
from infra.queries import insert_message
from infra.send_broadcast import read_input_broadcast_row

ERROR_TYPES = (
    "llm_error", "cc_error", "tool_boundary_violation", "tool_bridge_unavailable",
)


def _db():
    from infra.database import initialize_schema

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('t','u','T','x','x')"
    )
    return connection


def _error(
    db, message_id: str, kind: str, *, recovered: bool = False, turn_id: str = "turn-1",
) -> dict:
    metadata: dict[str, object] = {"type": kind}
    if recovered:
        metadata["recovered"] = True
    insert_turn_error(
        insert_message, "t", "boom", metadata, turn_id=turn_id, msg_id=message_id, db=db,
    )
    return {"id": message_id, "metadata": metadata}


def _turn(db, turn_id: str, status: str = "error") -> None:
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,"
        "ended_at,boot_epoch) VALUES (?,'t',0,'m',?,'x',?,'b')",
        (turn_id, status, None if status == "running" else "y"),
    )


def _is_turn_reference(value: object) -> bool:
    return isinstance(value, str) and len(value) == 6 and bool(REFERENCE.fullmatch(value))


def _queued_input(db) -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,"
        "delivery_state,input_order) VALUES ('queued','t','user','retry','{}','z','queued',1)"
    )


def test_writer_rows_store_references_and_emit_one_turn_error_log(caplog):
    from core.turn_error_surface import persist_llm_error
    from subagents import cc_event_simple, cc_runtime_errors, cc_shutdown
    from subagents import codex_runtime_errors, codex_shutdown_error

    with closing(_db()) as db:
        identity = SimpleNamespace(turn_id="turn-live", frame_fields=lambda: {})
        live = SimpleNamespace(corsia_turn=identity, tool_boundary_marker_id=None)
        broadcast = lambda *_args: None
        log = logging.getLogger(__name__)
        sink = logger.add(caplog.handler, level="INFO", format="{message}")
        try:
            persist_llm_error(
                lambda *_args, **_kwargs: nullcontext(db),
                insert_message,
                "t",
                "model",
                "turn-live",
                "native",
                "now",
                "native error",
                {"cause_kind": "usage_limit", "resets_at": "2099-01-01T00:00:00+00:00"},
            )
            cc_runtime_errors.surface_exit_error(
                "t", 1, "cc error", broadcast, db, identity,
                insert_message_fn=insert_message, logger=log,
            )
            cc_event_simple.record_tool_boundary_violation(
                "t", broadcast, db, {"type": "tool_boundary_violation"},
                "cc boundary", turn_id="turn-live", live=live,
            )
            codex_runtime_errors.surface_exit_error(
                "t", 1, "codex error", broadcast, db, identity,
                insert_message_fn=insert_message, logger=log, emit_terminal=False,
            )
            with patch.object(
                cc_shutdown,
                "insert_turn_error",
                side_effect=lambda *args, **kwargs: insert_turn_error(*args, db=db, **kwargs),
            ):
                cc_shutdown._record_shutdown_marker("t", None)
            with patch.object(
                codex_shutdown_error,
                "insert_turn_error",
                side_effect=lambda *args, **kwargs: insert_turn_error(*args, db=db, **kwargs),
            ):
                codex_shutdown_error.record_backend_shutdown_marker("t", None)
        finally:
            logger.remove(sink)

        rows = [json.loads(row["metadata"]) for row in db.execute(
            "SELECT metadata FROM messages WHERE thread_id='t' ORDER BY seq",
        )]

    assert [row["type"] for row in rows] == [
        "llm_error",
        "cc_error",
        "tool_boundary_violation",
        "cc_error",
        "cc_error",
        "cc_error",
    ]
    assert [row["turn_id"] for row in rows] == [
        "turn-live", "turn-live", "turn-live", "turn-live", None, None,
    ]
    assert all(_is_turn_reference(row["reference"]) for row in rows)
    assert (rows[0]["cause_kind"], rows[0]["resets_at"]) == ("usage_limit", "2099-01-01T00:00:00+00:00")
    assert rows[-1]["cause_kind"] == "backend_shutdown"
    lines = [record.getMessage() for record in caplog.records if "turn_error " in record.getMessage()]
    assert len(lines) == len(rows)
    for row in rows:
        assert sum(row["reference"] in line and f"turn_id={row['turn_id']}" in line for line in lines) == 1


def test_each_error_type_gets_one_stable_reference_and_log_line():
    with closing(_db()) as db:
        messages = [_error(db, f"e{index}", kind) for index, kind in enumerate(ERROR_TYPES)]
        lines: list[str] = []
        sink = logger.add(lines.append, format="{message}")
        try:
            _error(db, "logged", "llm_error")
        finally:
            logger.remove(sink)

        for message in messages:
            metadata = message["metadata"]
            assert metadata["turn_id"] == "turn-1"
            assert _is_turn_reference(metadata["reference"])
        assert len([line for line in lines if line.startswith("turn_error ")]) == 1


def test_projection_is_identical_for_history_and_live_rows():
    with closing(_db()) as db:
        _turn(db, "turn-1")
        message = _error(db, "error", "llm_error")
        _queued_input(db)
        history = [{"id": message["id"], "metadata": json.dumps(message["metadata"])}]

        finish_display(db, "t", history, include_tool_results=True)
        live = read_input_broadcast_row("error", db=db)

        assert history[0]["metadata"]["reference"] == live.metadata["reference"]
        assert history[0]["metadata"]["next_step"] == live.metadata["next_step"] == "held"


def test_next_step_is_recovered_or_failed_and_older_errors_are_not_held():
    with closing(_db()) as db:
        _turn(db, "turn-1")
        _turn(db, "turn-2")
        recovered = _error(db, "recovered", "tool_bridge_unavailable", recovered=True)
        old = _error(db, "old", "cc_error")
        newest = _error(db, "new", "tool_boundary_violation", turn_id="turn-2")
        _queued_input(db)

        apply_error_next_steps(db, "t", [old, newest, recovered])

        assert old["metadata"]["next_step"] == "failed"
        assert newest["metadata"]["next_step"] == "held"
        assert recovered["metadata"]["next_step"] == "recovered"


def test_stored_error_has_no_next_step_and_projects_failed_without_a_queue():
    with closing(_db()) as db:
        message = _error(db, "error", "llm_error")
        stored = json.loads(db.execute(
            "SELECT metadata FROM messages WHERE id='error'",
        ).fetchone()["metadata"])
        history = [{"id": message["id"], "metadata": json.dumps(message["metadata"])}]

        finish_display(db, "t", history, include_tool_results=True)

        assert "next_step" not in stored
        assert history[0]["metadata"]["next_step"] == "failed"


def test_recovered_next_step_uses_the_real_recovery_stamp():
    from infra.error_recovery import stamp_at_turn_start

    with closing(_db()) as db:
        message = _error(db, "error", "llm_error")
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
            "VALUES ('claim','t','system','wake','{\"type\":\"context_event\"}','z')",
        )
        claim = db.execute("SELECT * FROM messages WHERE id='claim'").fetchall()

        assert stamp_at_turn_start(db, "t", claim) == ["error"]
        history = [{
            "id": "error",
            "metadata": db.execute(
                "SELECT metadata FROM messages WHERE id='error'",
            ).fetchone()["metadata"],
        }]
        finish_display(db, "t", history, include_tool_results=True)
        assert history[0]["metadata"]["next_step"] == "recovered"


def test_held_applies_only_to_the_error_of_the_latest_failed_turn():
    with closing(_db()) as db:
        assert last_turn_error_id(db, "t") is None
        _error(db, "orphan", "cc_error", turn_id="no-such-turn")
        assert last_turn_error_id(db, "t") is None
        _turn(db, "turn-1")
        _error(db, "new", "llm_error")
        assert last_turn_error_id(db, "t") == "new"
        _turn(db, "turn-2", status="running")
        assert last_turn_error_id(db, "t") is None


@pytest.mark.asyncio
async def test_an_error_answered_by_a_new_message_never_reads_held_later(tmp_path, monkeypatch):
    """The person retried by sending a message: the error stays unrecovered, never held."""
    import infra.send_broadcast as send_broadcast
    import infra.state as state

    path = tmp_path / "answered.db"
    with closing(_db()) as db, closing(sqlite3.connect(path)) as target:
        _turn(db, "turn-1")
        error = _error(db, "error", "llm_error")
        _turn(db, "turn-2", status="completed")
        _turn(db, "turn-3", status="running")
        _queued_input(db)
        apply_error_next_steps(db, "t", [error])
        assert error["metadata"]["next_step"] == "failed"
        db.commit()
        db.backup(target)

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    sent = []
    monkeypatch.setattr(send_broadcast, "get_db", connect)
    monkeypatch.setattr(send_broadcast, "run_in_db", inline)
    monkeypatch.setattr(state, "broadcast_to_thread", lambda thread, payload: sent.append(payload))

    await send_broadcast.broadcast_newest_error("t")

    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("queued", "next_step"), [(True, "held"), (False, "failed")])
async def test_a_queue_change_re_broadcasts_the_newest_error(tmp_path, monkeypatch, queued, next_step):
    import infra.send_broadcast as send_broadcast
    import infra.state as state

    path = tmp_path / "held.db"
    with closing(_db()) as db, closing(sqlite3.connect(path)) as target:
        _turn(db, "turn-1")
        _error(db, "error", "llm_error")
        if queued:
            _queued_input(db)
        db.commit()
        db.backup(target)

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    sent = []
    monkeypatch.setattr(send_broadcast, "get_db", connect)
    monkeypatch.setattr(send_broadcast, "run_in_db", inline)
    monkeypatch.setattr(state, "broadcast_to_thread", lambda thread, payload: sent.append(payload))

    await send_broadcast.broadcast_newest_error("t")

    assert [payload["message"]["id"] for payload in sent] == ["error"]
    assert sent[0]["message"]["metadata"]["next_step"] == next_step


@pytest.mark.asyncio
async def test_cancelling_the_queued_input_re_broadcasts_the_newest_error(monkeypatch):
    from unittest.mock import AsyncMock

    import api.thread_inbox_actions as actions
    import infra.send_broadcast as send_broadcast
    import infra.thread_execution as thread_execution

    async def inline(fn, *args):
        if fn is actions._owned_input:
            return {}, {"delivery_revision": 1}
        return {"id": "queued"}

    rebroadcast = AsyncMock()
    monkeypatch.setattr(actions, "run_in_db", inline)
    monkeypatch.setattr(actions, "_broadcast", lambda _projected: None)
    monkeypatch.setattr(thread_execution, "drive_thread", AsyncMock())
    monkeypatch.setattr(send_broadcast, "broadcast_newest_error", rebroadcast)

    await actions.run_inbox_message_action("t", "queued", "u", action="cancel")

    rebroadcast.assert_awaited_once_with("t")


@pytest.mark.asyncio
async def test_a_failing_cancel_re_broadcast_still_drives_the_thread(monkeypatch):
    """A display projection never moves delivery: the next head is still driven."""
    from unittest.mock import AsyncMock

    import api.thread_inbox_actions as actions
    import infra.send_broadcast as send_broadcast
    import infra.thread_execution as thread_execution

    async def inline(fn, *args):
        if fn is actions._owned_input:
            return {}, {"delivery_revision": 1}
        return {"id": "queued"}

    async def locked(*_args):
        raise sqlite3.OperationalError("database is locked")

    drive = AsyncMock()
    monkeypatch.setattr(actions, "run_in_db", inline)
    monkeypatch.setattr(actions, "_broadcast", lambda _projected: None)
    monkeypatch.setattr(send_broadcast, "run_in_db", locked)
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    await actions.run_inbox_message_action("t", "queued", "u", action="cancel")

    drive.assert_awaited_once_with("t")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_outcome", "successor", "expected"),
    [("error", None, ["t"]), ("limited", None, ["t"]), ("completed", None, []), ("error", object(), [])],
)
async def test_a_failed_turn_settling_with_nothing_claimed_re_broadcasts_its_error(
    monkeypatch, probe_outcome, successor, expected,
):
    """The live frame went out while the Turn ran; settlement makes a queued input hold it."""
    import infra.thread_execution as thread_execution
    from infra.thread_driver import TerminalResult, TerminalSettlement

    rebroadcast: list[str] = []

    async def record(thread_id):
        rebroadcast.append(thread_id)

    async def admit(_successor):
        return None

    monkeypatch.setattr(thread_execution, "broadcast_newest_error", record)
    monkeypatch.setattr(thread_execution, "drive_turn_admission", admit)

    await thread_execution.drive_terminal_settlement(TerminalSettlement(
        TerminalResult.APPLIED, "t", successor=successor, probe_outcome=probe_outcome,
    ))

    assert rebroadcast == expected


def test_a_thread_whose_latest_turn_did_not_fail_reads_no_messages():
    with closing(_db()) as db:
        _turn(db, "turn-1")
        _error(db, "error", "llm_error")
        _turn(db, "turn-2", status="completed")
        statements: list[str] = []
        db.set_trace_callback(statements.append)

        assert last_turn_error_id(db, "t") is None

        assert len(statements) == 1
        assert "messages" not in statements[0]


def test_last_turn_error_queries_seek_their_indexes_without_sorting():
    with closing(_db()) as db:
        placeholders = ",".join("?" for _ in ERROR_TYPES)
        for sql, params, index in (
            (_LATEST_TURN_SQL, ("t",), "idx_active_turns_session"),
            (_LAST_TURN_ERROR_SQL.format(placeholders=placeholders), ("t", *ERROR_TYPES),
             "idx_messages_session_seq"),
        ):
            plan = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            detail = " ".join(str(row[3]) for row in plan)
            assert index in detail
            assert "TEMP B-TREE" not in detail
