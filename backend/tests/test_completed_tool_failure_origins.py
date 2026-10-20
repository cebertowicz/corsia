"""Post-effect wrappers preserve causes and emit content-safe diagnostics."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from core.checkpoint_fork_kind import CHECKPOINT_KIND

from tools.recorded_call import (
    CompletedToolCall,
    CompletedToolFailure,
    combine_completed_failures,
    completed_tool_failure,
    enrich_completed_failure,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.parametrize("cause_type", [RuntimeError, asyncio.CancelledError])
async def test_foreground_publication_failure_keeps_cause_and_harness_identity(
    caplog, harness, cause_type,
):
    """The shared foreground publication boundary is a wrapper, not a bypass,
    for both a generic exception and a cancellation while canonical is absent."""
    from subagents.harness_tool_publication import _foreground_result

    cause = cause_type("foreground publication boundary failed")

    async def persist_delivery(**_kwargs):
        raise cause

    async def media_delivery(_delivery, _model):
        return None

    completion = CompletedToolCall(
        name="read_file", result="private foreground result",
        invocation_id="call-1", provider_tool_call_id="call-1",
        thread_id="thread-1", turn_id="turn-1",
    )
    with caplog.at_level(logging.ERROR, logger="tools.recorded_call"):
        with pytest.raises(CompletedToolFailure) as failure:
            await _foreground_result(
                completion, thread_id="thread-1",
                turn_identity=SimpleNamespace(turn_id="turn-1"),
                model=None, persist_delivery=persist_delivery,
                media_delivery=media_delivery, decorate_result=None,
                harness=harness,
            )

    error = failure.value
    assert error.__cause__ is cause
    assert error.completed[0].result == "private foreground result"
    assert error.completed[0].canonical is None
    [record] = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is cause
    message = record.getMessage()
    assert all(value in message for value in (
        harness, "thread-1", "turn-1", "read_file", "call-1",
        "foreground publication boundary failed",
    ))
    assert "private foreground result" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_checkpoint_publication_failure_keeps_cause_and_harness_identity(
    monkeypatch, caplog, harness,
):
    """The shared checkpoint-media publication boundary is a wrapper, not a
    bypass, covering the same (Exception, CancelledError) catch."""
    from subagents import harness_tool_publication

    cause = RuntimeError("checkpoint media boundary failed")

    class _Result(str):
        media = object()

    def raising_media(*_args, **_kwargs):
        raise cause

    completion = CompletedToolCall(
        name="read_file", result=_Result("private checkpoint result"),
        invocation_id="call-1", provider_tool_call_id="call-1",
        thread_id="thread-1", turn_id="turn-1", checkpoint_id="checkpoint-1",
    )
    from core import media_delivery

    monkeypatch.setattr(media_delivery, "tool_result_media_for_reference", raising_media)
    with caplog.at_level(logging.ERROR, logger="tools.recorded_call"):
        with pytest.raises(CompletedToolFailure) as failure:
            await harness_tool_publication._checkpoint_result(
                completion, None, harness=harness,
            )

    error = failure.value
    assert error.__cause__ is cause
    assert error.completed[0].result == "private checkpoint result"
    [record] = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is cause
    message = record.getMessage()
    assert all(value in message for value in (
        harness, "thread-1", "turn-1", "read_file", "call-1",
        "checkpoint media boundary failed",
    ))
    assert "private checkpoint result" not in message


@pytest.mark.asyncio
async def test_checkpoint_native_tool_loop_failure_keeps_cause(monkeypatch, caplog):
    """The Native checkpoint tool loop is a wrapper, not a bypass, like the
    other harness_tool_publication boundaries above."""
    from core import checkpoint_native_tools

    cause = RuntimeError("native checkpoint boundary failed")

    class _RecordingTools:
        def __init__(self, result):
            self.calls = 0
            self._result = result

        async def execute(self, _name, _arguments):
            self.calls += 1
            return self._result

    tools = _RecordingTools("private native checkpoint result")
    agent = SimpleNamespace(tools=tools)
    response = SimpleNamespace(
        content="", reasoning_content=None,
        tool_calls=[
            SimpleNamespace(id="call-1", name="continuity_edit", arguments={}),
            SimpleNamespace(id="call-2", name="continuity_edit", arguments={}),
        ],
    )
    request = SimpleNamespace(thread_id="thread-1", checkpoint_id="checkpoint-1")
    fits_calls = 0

    async def failing_fits(_request, _messages, _tools):
        nonlocal fits_calls
        fits_calls += 1
        if fits_calls == 1:
            return True
        raise cause

    monkeypatch.setattr(checkpoint_native_tools, "input_fits_off_loop", failing_fits)
    with caplog.at_level(logging.ERROR, logger="tools.recorded_call"):
        with pytest.raises(CompletedToolFailure) as failure:
            await checkpoint_native_tools.run_tool_calls(
                agent, response, [], asyncio.Event(), request, [], kind=CHECKPOINT_KIND,
            )

    error = failure.value
    assert error.__cause__ is cause
    assert tools.calls == 1
    assert error.completed[0].result == "private native checkpoint result"
    [record] = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is cause
    message = record.getMessage()
    assert all(value in message for value in (
        "native", "thread-1", "continuity_edit", "call-1", "native checkpoint boundary failed",
    ))
    assert "private native checkpoint result" not in message


def test_batch_diagnostic_names_count_and_last_completed_call(caplog) -> None:
    """A batch-level failure describes the batch and its last completed call."""
    cause = RuntimeError("batch publication failed")
    completed = (
        CompletedToolCall(
            "read_file", "private first result", thread_id="thread-1",
            turn_id="turn-1", provider_tool_call_id="call-1",
        ),
        CompletedToolCall(
            "write_file", "private last result", thread_id="thread-1",
            turn_id="turn-1", provider_tool_call_id="call-2",
        ),
    )

    with caplog.at_level(logging.ERROR, logger="tools.recorded_call"):
        failure = completed_tool_failure(completed, cause, harness="native")

    [record] = caplog.records
    message = record.getMessage()
    assert all(value in message for value in (
        "completed_count=2", "last_tool='write_file'", "last_call='call-2'",
        "batch publication failed",
    ))
    assert "private first result" not in message
    assert "private last result" not in message


def test_combined_failures_keep_visible_detail_and_earliest_cause() -> None:
    """A later user-facing reason remains visible without replacing the cause."""
    known = CompletedToolFailure((CompletedToolCall("read_file", "first result"),))
    incoming = CompletedToolFailure(
        (CompletedToolCall("write_file", "later result"),), "checkpoint refused",
    )
    known.__cause__ = RuntimeError("first retained failure")

    combined = combine_completed_failures(known, incoming)

    assert combined.detail == "checkpoint refused"
    assert combined.__cause__ is known.__cause__
    assert [call.result for call in combined.completed] == ["first result", "later result"]


def test_combined_failures_keep_the_first_visible_detail() -> None:
    """A later reason never replaces an earlier user-facing reason."""
    known = CompletedToolFailure((CompletedToolCall("read_file", "first"),), "first reason")
    incoming = CompletedToolFailure((CompletedToolCall("write_file", "later"),), "later reason")

    assert combine_completed_failures(known, incoming).detail == "first reason"


def test_enrich_completed_failure_name_conflict_returns_the_original() -> None:
    """A producer/outer name conflict keeps the original failure and cause."""
    completed = (CompletedToolCall("read_file", "private original result"),)
    failure = CompletedToolFailure(completed, "original detail")
    cause = RuntimeError("original cause")
    failure.__cause__ = cause

    result = enrich_completed_failure(
        failure, name="write_file", arguments={}, thread_id="thread-1",
    )

    assert result is failure
    assert result.completed == completed
    assert result.__cause__ is cause
    assert result.detail == "original detail"
