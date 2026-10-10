"""Dispatch bookkeeping failures stay visible and never lose call ownership.

These tests patch `plans.public.record_tool_dispatch` / `settle_tool_dispatch`, which the registry
resolves at call time. A call under selected work does not start without its
ownership record: a recorder failure reports an error and the tool never runs,
and a row the failing recorder may already have written is settled by id.

A settlement failure of any class after a completed call never fails that call:
the effect already landed, so the failure is logged with its traceback and the
completed result stands, while the still-open row is closed by Turn-terminal
settlement. A cancellation during that settlement raises the completed-call
failure that carries the result. After a failed call the error names both
failures; after cancellation of the call itself the failure is logged and the
cancellation propagates.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest
from loguru import logger as loguru_logger

from infra.database import get_db
from plans import service, tool_dispatches
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ERROR_HINT,
    BlockingProbe,
    ProbeTool,
    abandon_blocking_call,
    dispatch_rows,
    execute_as,
    open_turn,
    registry_of,
    selected_plan,
    start_blocking_call,
)


#: The real recorder, captured before any test replaces the module attribute.
_ORIGINAL_RECORD = tool_dispatches.record


class _Broken:
    """A dispatch bookkeeping call that fails with one recognizable error."""

    def __init__(self, message: str, error_type: type[Exception] = RuntimeError) -> None:
        self._message = message
        self._error_type = error_type
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise self._error_type(self._message)


class _LoguruErrors:
    """Collect ERROR-or-worse loguru records alongside std logging's caplog."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._sink_id: int | None = None

    def __enter__(self) -> "_LoguruErrors":
        self._sink_id = loguru_logger.add(self._write, level="ERROR")
        return self

    def __exit__(self, *_exc) -> None:
        loguru_logger.remove(self._sink_id)

    def _write(self, message) -> None:
        self.messages.append(str(message))


def _error_entries(caplog, loguru_errors: _LoguruErrors) -> list[str]:
    """Return each error-level diagnostic as its message plus exception text."""
    entries = []
    for record in caplog.records:
        if record.levelno < logging.ERROR:
            continue
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        entries.append(text)
    return entries + loguru_errors.messages


def _selected_executor(db, name: str):
    thread_id = mk_session(name)
    identity = open_turn(db, thread_id)
    _plan, selected = selected_plan(db, thread_id)
    return thread_id, identity, selected["admission"]["selection"]["attempt_id"]


@pytest.mark.asyncio
async def test_a_recorder_failure_does_not_start_the_tool(
    isolated_db, monkeypatch, caplog,
):
    thread_id, identity, _attempt = _selected_executor(get_db(), "recorder-down")
    broken = _Broken("dispatch recorder unavailable")
    monkeypatch.setattr("plans.public.record_tool_dispatch", broken)
    tool = ProbeTool(result="probe result bytes")
    caplog.set_level(logging.ERROR)

    with _LoguruErrors() as loguru_errors:
        result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result.startswith(f"Error executing {tool.name}: ")
    assert "dispatch recorder unavailable" in result
    assert tool.calls == 0
    assert broken.calls == 1
    assert dispatch_rows(get_db()) == []
    entries = _error_entries(caplog, loguru_errors)
    assert any(
        tool.name in entry and thread_id in entry
        and "dispatch recorder unavailable" in entry
        for entry in entries
    ), entries


def _record_then_fail(*args, **kwargs):
    """Commit the row, then fail as if the recorder's reply were lost."""
    _ORIGINAL_RECORD(*args, **kwargs)
    raise RuntimeError("dispatch recorder reply lost")


@pytest.mark.asyncio
async def test_a_row_written_before_a_recorder_failure_is_settled_by_id(
    isolated_db, monkeypatch,
):
    thread_id, identity, attempt_id = _selected_executor(get_db(), "recorder-lost")
    monkeypatch.setattr("plans.public.record_tool_dispatch", _record_then_fail)
    tool = ProbeTool(result="probe result bytes")

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result.startswith(f"Error executing {tool.name}: ")
    assert tool.calls == 0
    [dispatch] = dispatch_rows(get_db())
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["settled_at"] is not None
    assert dispatch["outcome"] == "failed"


class _RaisingProbe(ProbeTool):
    """A probe whose started call fails with one recognizable error."""

    async def execute(self, **_kwargs) -> str:
        self.calls += 1
        raise RuntimeError("probe effect failed")


def _assert_open_until_turn_terminal(attempt_id: str, turn_id: str) -> None:
    """The unsettled row stays open, and Turn-terminal settlement closes it."""
    db = get_db()
    [dispatch] = dispatch_rows(db)
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["settled_at"] is None
    with service._write(db):
        tool_dispatches.settle_turn(turn_id, db=db)
    [closed] = dispatch_rows(db)
    assert closed["settled_at"] is not None
    assert closed["outcome"] == "unknown"
    db.close()


def _logged(caplog, loguru_errors: _LoguruErrors, *fragments: str) -> None:
    """Assert one error-level diagnostic carries every fragment."""
    entries = _error_entries(caplog, loguru_errors)
    assert any(all(part in entry for part in fragments) for entry in entries), entries


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, RuntimeError])
async def test_a_settlement_failure_after_a_completed_call_does_not_fail_the_turn(
    isolated_db, monkeypatch, caplog, error_type,
):
    """A failed plan-dispatch settlement is not the Turn's failure, whatever its class.

    The effect already landed, so the completed result stands, the failure is
    logged once with its identities and traceback, and the still-open row is
    closed by Turn-terminal settlement.
    """
    thread_id, identity, attempt_id = _selected_executor(get_db(), "settle-failed")
    broken = _Broken("dispatch settlement unavailable", error_type)
    tool = ProbeTool(result="probe result bytes")
    caplog.set_level(logging.ERROR)

    with monkeypatch.context() as patched:
        patched.setattr("plans.public.settle_tool_dispatch", broken)
        result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "probe result bytes"
    assert tool.calls == 1
    assert broken.calls == 1
    dispatch_id = dispatch_rows(get_db())[0]["id"]
    failures = [
        record for record in caplog.records
        if record.levelno == logging.ERROR
        and "plan dispatch settlement failed" in record.getMessage()
    ]
    assert len(failures) == 1, [record.getMessage() for record in caplog.records]
    assert failures[0].exc_info is not None
    text = failures[0].getMessage()
    for fragment in (
        f"tool={tool.name}",
        f"call={dispatch_id[:8]}",
        f"turn={identity.turn_id[:8]}",
        "outcome=succeeded",
    ):
        assert fragment in text, text
    _assert_open_until_turn_terminal(attempt_id, identity.turn_id)


@pytest.mark.asyncio
async def test_a_cancelled_settlement_after_a_completed_call_carries_the_result(
    isolated_db, monkeypatch, caplog,
):
    """Stop during settlement raises the failure that keeps the completed call."""
    thread_id, identity, attempt_id = _selected_executor(get_db(), "settle-failed-cancel")
    cancelled = _Broken("settlement cancelled", asyncio.CancelledError)
    tool = ProbeTool(result="probe result bytes")
    caplog.set_level(logging.ERROR)

    with monkeypatch.context() as patched, _LoguruErrors() as loguru_errors:
        patched.setattr("plans.public.settle_tool_dispatch", cancelled)
        with pytest.raises(RuntimeError) as raised:
            await execute_as(registry_of(tool), identity, thread_id, tool.name)

    from tools.recorded_call import CompletedToolFailure

    assert isinstance(raised.value, CompletedToolFailure)
    [completed] = raised.value.completed
    assert completed.result == "probe result bytes"
    assert completed.name == tool.name
    assert completed.thread_id == thread_id
    assert completed.turn_id == identity.turn_id
    assert completed.dispatch_id == dispatch_rows(get_db())[0]["id"]
    assert completed.canonical is None
    assert "probe result bytes" not in str(raised.value)
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert tool.calls == 1
    assert cancelled.calls == 1
    entries = _error_entries(caplog, loguru_errors)
    identity_entries = [
        entry for entry in entries
        if "completed tool failure" in entry and "harness=recorded-call" in entry
        and thread_id in entry and identity.turn_id in entry and tool.name in entry
        and dispatch_rows(get_db())[0]["id"] in entry
    ]
    assert len(identity_entries) == 1, entries
    _assert_open_until_turn_terminal(attempt_id, identity.turn_id)


@pytest.mark.asyncio
async def test_a_settlement_failure_returns_the_completed_media_result(isolated_db, monkeypatch):
    from core.media_reads import MediaToolResult

    thread_id, identity, _attempt = _selected_executor(get_db(), "settle-media")
    reference = object()
    tool = ProbeTool(result=MediaToolResult("api_key=synthetic-secret-value", reference))
    monkeypatch.setattr("plans.public.settle_tool_dispatch", _Broken("api_key=synthetic-cause-value", sqlite3.OperationalError))
    returned = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    # The failure changes neither the result the tool produced nor its media.
    assert getattr(returned, "media", None) is reference
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_a_settlement_failure_after_a_failed_call_names_both_failures(
    isolated_db, monkeypatch, caplog,
):
    thread_id, identity, attempt_id = _selected_executor(get_db(), "settle-after-fail")
    broken = _Broken("dispatch settlement unavailable")
    tool = _RaisingProbe()
    caplog.set_level(logging.ERROR)

    with monkeypatch.context() as patched, _LoguruErrors() as loguru_errors:
        patched.setattr("plans.public.settle_tool_dispatch", broken)
        result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == (
        f"Error executing {tool.name}: {tool.name} failed: probe effect failed; "
        "its plan dispatch could not be settled either: "
        "dispatch settlement unavailable" + ERROR_HINT
    )
    assert tool.calls == 1
    assert broken.calls == 1
    _logged(caplog, loguru_errors, tool.name, "dispatch settlement unavailable")
    _assert_open_until_turn_terminal(attempt_id, identity.turn_id)


@pytest.mark.asyncio
async def test_a_settlement_failure_after_cancellation_lets_the_cancellation_propagate(
    isolated_db, monkeypatch, caplog,
):
    """Stop is never turned into a settlement error; the failure is only logged."""
    thread_id, identity, attempt_id = _selected_executor(get_db(), "settle-after-cancel")
    broken = _Broken("dispatch settlement unavailable")
    probe = BlockingProbe()
    caplog.set_level(logging.ERROR)

    with monkeypatch.context() as patched, _LoguruErrors() as loguru_errors:
        patched.setattr("plans.public.settle_tool_dispatch", broken)
        call = await start_blocking_call(registry_of(probe), identity, thread_id, probe)
        call.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            await abandon_blocking_call(probe, call)

    assert probe.calls == 1
    assert broken.calls == 1
    _logged(caplog, loguru_errors, probe.name, "dispatch settlement unavailable")
    _assert_open_until_turn_terminal(attempt_id, identity.turn_id)
