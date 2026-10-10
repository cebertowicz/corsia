from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from core import turns_steps
from core.turns_steps import (
    USER_INTERRUPTION_TOOL_RESULT,
    _execute_tool_calls_inner,
)
from infra.queries import get_thread_project_id
from tools.recorded_call import CompletedToolFailure


class _ProjectIdCursor:
    def __init__(
        self, row: dict[str, str | None] | None, error: BaseException | None = None,
    ) -> None:
        self._row = row
        self._error = error

    def fetchone(self) -> dict[str, str | None] | None:
        if self._error is not None:
            raise self._error
        return self._row


class _ProjectIdConnection:
    def __init__(
        self,
        row: dict[str, str | None] | None,
        *,
        execute_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._row = row
        self._execute_error = execute_error
        self._fetch_error = fetch_error
        self._close_error = close_error
        self.closed = False
        self.executed = False

    def execute(self, _query: str, _params: tuple[str]) -> _ProjectIdCursor:
        self.executed = True
        if self._execute_error is not None:
            raise self._execute_error
        return _ProjectIdCursor(self._row, self._fetch_error)

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FalseyProjectIdConnection(_ProjectIdConnection):
    def __bool__(self) -> bool:
        return False


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"project_id": "project-1"}, "project-1"),
        (None, None),
        ({"project_id": None}, None),
    ],
)
def test_get_thread_project_id_without_db_preserves_results_and_closes(
    monkeypatch, row, expected,
) -> None:
    connection = _ProjectIdConnection(row)
    monkeypatch.setattr("infra.queries.get_db", lambda: connection)

    assert get_thread_project_id("thread-1") == expected
    assert connection.closed is True


def test_get_thread_project_id_with_falsey_db_uses_supplied_connection(
    monkeypatch,
) -> None:
    connection = _FalseyProjectIdConnection({"project_id": "project-1"})

    def fail_get_db():
        raise AssertionError("get_db should not run for caller-supplied db")

    monkeypatch.setattr("infra.queries.get_db", fail_get_db)

    assert get_thread_project_id("thread-1", db=connection) == "project-1"
    assert connection.executed is True
    assert connection.closed is False


@pytest.mark.parametrize(
    ("execute_error", "fetch_error"),
    [
        (sqlite3.Error("execute failed"), None),
        (None, sqlite3.Error("fetch failed")),
    ],
)
def test_get_thread_project_id_without_db_propagates_and_closes_on_query_error(
    monkeypatch, execute_error, fetch_error,
) -> None:
    connection = _ProjectIdConnection(
        None, execute_error=execute_error, fetch_error=fetch_error,
    )
    monkeypatch.setattr("infra.queries.get_db", lambda: connection)

    with pytest.raises(sqlite3.Error, match="failed"):
        get_thread_project_id("thread-1")
    assert connection.closed is True


def test_get_thread_project_id_without_db_propagates_close_error(monkeypatch) -> None:
    connection = _ProjectIdConnection(
        {"project_id": "project-1"},
        close_error=sqlite3.Error("close failed"),
    )
    monkeypatch.setattr("infra.queries.get_db", lambda: connection)

    with pytest.raises(sqlite3.Error, match="close failed"):
        get_thread_project_id("thread-1")
    assert connection.closed is True


def test_thread_project_lookup_exports_canonical_adapter() -> None:
    assert "lookup_thread_project_id" in turns_steps.__all__


def test_thread_project_lookup_delegates_without_a_connection_argument(
    monkeypatch,
) -> None:
    calls: list[tuple[str]] = []

    def lookup(thread_id: str) -> str:
        calls.append((thread_id,))
        return "project-1"

    monkeypatch.setattr(turns_steps, "get_thread_project_id", lookup)

    assert asyncio.run(turns_steps.lookup_thread_project_id("thread-1")) == "project-1"
    assert calls == [("thread-1",)]


def test_thread_project_lookup_keeps_sqlite_error_as_advisory_none(monkeypatch) -> None:
    warning_calls: list[tuple] = []

    def failing_lookup(_thread_id: str) -> None:
        raise sqlite3.Error("lookup failed")

    monkeypatch.setattr(turns_steps, "get_thread_project_id", failing_lookup)
    monkeypatch.setattr(turns_steps.logger, "warning", lambda *args: warning_calls.append(args))

    assert asyncio.run(turns_steps.lookup_thread_project_id("thread-1")) is None
    assert len(warning_calls) == 1
    assert warning_calls[0][:2] == (
        "lookup_thread_project_id failed for thread_id={!r}: {!r}",
        "thread-1",
    )
    assert repr(warning_calls[0][2]) == "Error('lookup failed')"


def test_mixed_native_response_is_one_step_and_unstarted_call_is_structural_cancel() -> None:
    cancel = asyncio.Event()
    events: list[tuple[str, dict]] = []

    class _Tools:
        async def execute(self, name: str, arguments: dict) -> str:
            cancel.set()
            return "first real result"

    class _Context:
        @staticmethod
        def add_assistant_message(messages, content, calls, **kwargs):
            return [*messages, {"role": "assistant", "content": content, "tool_calls": calls}]

        @staticmethod
        def add_tool_result(messages, tool_id, name, result):
            return [*messages, {
                "role": "tool", "tool_call_id": tool_id,
                "name": name, "content": result,
            }]

    agent = SimpleNamespace(
        tools=_Tools(),
        context=_Context(),
        _strip_think=lambda value: value,
        _tool_hint=lambda calls: calls[0].name,
    )
    response = SimpleNamespace(
        content="I will run two tools.",
        reasoning_content=None,
        tool_calls=[
            SimpleNamespace(id="call-1", name="Read", arguments={"path": "a"}),
            SimpleNamespace(id="call-2", name="Read", arguments={"path": "b"}),
        ],
    )

    async def on_step(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    asyncio.run(_execute_tool_calls_inner(
        agent, response, [], "thread-1", cancel, on_step, [], "model-1",
    ))

    assert [kind for kind, _ in events].count("text") == 0
    start = next(payload for kind, payload in events if kind == "tool_calls_start")
    assert start["content"] == "I will run two tools."
    completions = [payload for kind, payload in events if kind == "tool_complete"]
    assert completions[0]["result"] == "first real result"
    assert completions[0].get("synthetic_tool_result") is not True
    assert completions[1] == {
        "id": "call-2",
        "name": "Read",
        "result": USER_INTERRUPTION_TOOL_RESULT,
        "synthetic_tool_result": True,
        "synthetic_reason": "user_interruption",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_event", ["tool_complete", "tool_calls_done"])
async def test_native_post_completion_failure_keeps_its_cause(
    monkeypatch, caplog, failing_event,
) -> None:
    """Both Native publication wrappers preserve the completed effect's cause."""
    cause = RuntimeError("native post-completion boundary failed")

    class _Tools:
        async def execute(self, _name: str, _arguments: dict) -> str:
            return "full result"

    class _Context:
        @staticmethod
        def add_assistant_message(messages, content, calls, **_kwargs):
            return [*messages, {"role": "assistant", "content": content, "tool_calls": calls}]

    agent = SimpleNamespace(
        tools=_Tools(), context=_Context(), _strip_think=lambda value: value,
        _tool_hint=lambda calls: calls[0].name,
    )
    response = SimpleNamespace(
        content="", reasoning_content=None,
        tool_calls=[SimpleNamespace(id="call-1", name="read_file", arguments={})],
    )

    async def on_step(kind: str, _payload: dict):
        if kind == failing_event:
            raise cause
        return None

    monkeypatch.setattr(
        turns_steps, "_current_turn_identity",
        lambda: SimpleNamespace(turn_id="turn-1"),
    )
    with caplog.at_level(logging.ERROR, logger="tools.recorded_call"):
        with pytest.raises(CompletedToolFailure) as failure:
            await _execute_tool_calls_inner(
                agent, response, [], "thread-1", asyncio.Event(), on_step, [], "model-1",
            )

    error = failure.value
    assert error.__cause__ is cause
    assert error.completed[0].result == "full result"
    [record] = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is cause
    message = record.getMessage()
    assert all(value in message for value in (
        "native", "thread-1", "turn-1", "read_file", "call-1",
        "native post-completion boundary failed",
    ))
    assert "full result" not in message
