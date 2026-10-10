"""Tool-call ownership reports every outcome as a ``plan_dispatch`` product event.

``plan_tool_dispatches`` is the state. The events are the rollout evidence the
rows cannot give: a call that ran without a row and why, a recording or
settlement failure, and the calls a Turn terminal had to close as unknown. The
tool boundary emits after the write it reports; the terminal settlement driver
emits after the terminal write commits. Every event is written off the event
loop, because a product event is a synchronous analytics write. Plans' own
tools emit nothing, and no event carries a tool's arguments or result.
"""

from __future__ import annotations

import json
import threading

import pytest

from infra.database import get_db
from infra.thread_driver import TerminalResult, finish_turn
from infra.thread_execution import drive_terminal_settlement
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    BlockingProbe,
    ProbeTool,
    abandon_blocking_call,
    execute_as,
    open_turn,
    read_file_probe,
    registry_of,
    selected_plan,
    start_blocking_call,
)
from tests.test_clean_turn_terminal import _db, _running_turn, _session
from tests.test_plan_turn_terminal_settlement import _open_dispatch, _selected_task

#: The only properties a ``plan_dispatch`` event may carry.
_ALLOWED_PROPERTIES = {"tool", "reason", "outcome", "count"}


@pytest.fixture
def emitted(mock_emit) -> list[tuple]:
    """Capture every product event as (name, thread id, properties)."""
    return mock_emit.full


@pytest.fixture
def emit_threads(monkeypatch) -> list[tuple[str, int]]:
    """Capture each ``plan_dispatch`` emit with the OS thread that wrote it."""
    from infra import product_events

    captured: list[tuple[str, int]] = []

    def capture(category, action, *, thread_id=None, user_id=None, properties=None):
        if category == "plan_dispatch":
            captured.append((action, threading.get_ident()))

    monkeypatch.setattr(product_events, "emit_product_event", capture)
    return captured


def _dispatch_events(emitted: list[tuple]) -> list[tuple]:
    return [event for event in emitted if event[0].startswith("plan_dispatch.")]


class _RaisingProbe(ProbeTool):
    """A probe whose started call fails."""

    async def execute(self, **_kwargs) -> str:
        self.calls += 1
        raise RuntimeError("probe effect failed")


class _Broken:
    """A dispatch bookkeeping call that always fails."""

    def __call__(self, *_args, **_kwargs):
        raise RuntimeError("dispatch bookkeeping unavailable")


def _selected_executor(name: str):
    """Return a thread with a live Turn and one valid selection."""
    thread_id = mk_session(name)
    db = get_db()
    identity = open_turn(db, thread_id)
    selected_plan(db, thread_id)
    db.close()
    return thread_id, identity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "outcome"),
    [
        (ProbeTool(result="ok"), "succeeded"),
        (ProbeTool(result="Error: the tool refused"), "refused"),
        (_RaisingProbe(), "failed"),
    ],
    ids=["succeeded", "refused", "failed"],
)
async def test_a_call_under_selected_work_reports_recorded_then_its_settlement(
    isolated_db, emitted, tool, outcome,
):
    thread_id, identity = _selected_executor(f"dispatch-events-{outcome}")

    await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert _dispatch_events(emitted) == [
        ("plan_dispatch.recorded", thread_id, {"tool": tool.name}),
        ("plan_dispatch.settled", thread_id, {"tool": tool.name, "outcome": outcome}),
    ]


@pytest.mark.asyncio
async def test_a_stopped_call_reports_its_settlement_as_unknown(isolated_db, emitted):
    thread_id, identity = _selected_executor("dispatch-events-stopped")
    probe = BlockingProbe()
    call = await start_blocking_call(registry_of(probe), identity, thread_id, probe)

    call.cancel()
    await abandon_blocking_call(probe, call)

    assert _dispatch_events(emitted) == [
        ("plan_dispatch.recorded", thread_id, {"tool": probe.name}),
        ("plan_dispatch.settled", thread_id, {"tool": probe.name, "outcome": "unknown"}),
    ]


@pytest.mark.asyncio
async def test_a_call_without_a_valid_selection_reports_why_it_ran_unrecorded(
    isolated_db, emitted,
):
    thread_id = mk_session("dispatch-events-no-selection")
    db = get_db()
    identity = open_turn(db, thread_id)
    db.close()
    tool = ProbeTool()

    assert await execute_as(registry_of(tool), identity, thread_id, tool.name) == "ok"

    assert _dispatch_events(emitted) == [
        ("plan_dispatch.unrecorded", thread_id,
         {"tool": tool.name, "reason": "no_selection"}),
    ]


@pytest.mark.asyncio
async def test_a_call_without_turn_identity_reports_no_turn(isolated_db, emitted):
    thread_id, _identity = _selected_executor("dispatch-events-no-turn")
    tool = ProbeTool()

    assert await execute_as(registry_of(tool), None, thread_id, tool.name) == "ok"

    assert _dispatch_events(emitted) == [
        ("plan_dispatch.unrecorded", thread_id, {"tool": tool.name, "reason": "no_turn"}),
    ]


@pytest.mark.asyncio
async def test_plans_own_tools_report_nothing(isolated_db, emitted):
    thread_id, identity = _selected_executor("dispatch-events-plans-tool")
    plans_tool = ProbeTool("plan_read", result="plan_read ran")

    await execute_as(registry_of(plans_tool), identity, thread_id, plans_tool.name)

    assert plans_tool.calls == 1
    assert _dispatch_events(emitted) == []


@pytest.mark.asyncio
async def test_a_recorder_failure_reports_record_failed_and_no_settlement(
    isolated_db, emitted, monkeypatch,
):
    thread_id, identity = _selected_executor("dispatch-events-record-failed")
    monkeypatch.setattr("plans.public.record_tool_dispatch", _Broken())
    tool = ProbeTool()

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result.startswith(f"Error executing {tool.name}: ")
    assert tool.calls == 0
    assert _dispatch_events(emitted) == [
        ("plan_dispatch.record_failed", thread_id, {"tool": tool.name}),
    ]


@pytest.mark.asyncio
async def test_a_settlement_failure_reports_settle_failed_with_its_outcome(
    isolated_db, emitted, monkeypatch,
):
    """A failed settlement reports its outcome and leaves the Turn running."""
    thread_id, identity = _selected_executor("dispatch-events-settle-failed")
    tool = ProbeTool()

    with monkeypatch.context() as patched:
        patched.setattr("plans.public.settle_tool_dispatch", _Broken())
        result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "ok"

    assert _dispatch_events(emitted) == [
        ("plan_dispatch.recorded", thread_id, {"tool": tool.name}),
        ("plan_dispatch.settle_failed", thread_id,
         {"tool": tool.name, "outcome": "succeeded"}),
    ]


@pytest.mark.asyncio
async def test_no_dispatch_event_carries_tool_arguments_or_results(isolated_db, emitted):
    thread_id, identity = _selected_executor("dispatch-events-content")
    tool = read_file_probe(result="private result text")

    await execute_as(
        registry_of(tool), identity, thread_id, tool.name,
        {"path": "notes/private-argument.md"},
    )

    events = _dispatch_events(emitted)
    assert [name for name, _thread, _properties in events] == [
        "plan_dispatch.recorded", "plan_dispatch.settled",
    ]
    for _name, _thread, properties in events:
        assert set(properties) <= _ALLOWED_PROPERTIES
    serialized = json.dumps(events)
    assert "private-argument" not in serialized
    assert "private result text" not in serialized


def _stopped_with_open_calls(db, thread_id: str, open_calls: int):
    """Stop a Turn working selected work while ``open_calls`` calls are still open."""
    _session(db, thread_id)
    _plan, _node, attempt_id = _selected_task(db, thread_id)
    turn_id = f"{thread_id}-turn"
    db.commit()
    _running_turn(db, thread_id, turn_id)
    for index in range(open_calls):
        _open_dispatch(db, attempt_id, turn_id, dispatch_id=f"{thread_id}-call-{index}")
    return finish_turn(
        turn_id, outcome="cancelled", ended_at="t4",
        successor_turn_id=f"{turn_id}-next", boot_epoch="boot-1", db=db,
    )


@pytest.mark.asyncio
async def test_a_turn_terminal_reports_how_many_calls_it_settled_as_unknown(emitted):
    db = _db()
    try:
        db.commit()
        settlement = _stopped_with_open_calls(db, "terminal-unknown", open_calls=2)
        assert settlement.result is TerminalResult.APPLIED
        assert settlement.plan_dispatches_unknown == 2
        assert _dispatch_events(emitted) == []

        await drive_terminal_settlement(settlement)
    finally:
        db.close()

    assert _dispatch_events(emitted) == [
        ("plan_dispatch.terminal_unknown", "terminal-unknown", {"count": 2}),
    ]


@pytest.mark.asyncio
async def test_a_turn_terminal_with_no_open_call_reports_nothing(emitted):
    db = _db()
    try:
        db.commit()
        settlement = _stopped_with_open_calls(db, "terminal-clean", open_calls=0)
        assert settlement.plan_dispatches_unknown == 0

        await drive_terminal_settlement(settlement)
    finally:
        db.close()

    assert _dispatch_events(emitted) == []


@pytest.mark.asyncio
async def test_no_dispatch_event_is_written_on_the_event_loop(isolated_db, emit_threads):
    """Tool-boundary and Turn-terminal events both run in the database worker."""
    loop_thread = threading.get_ident()
    thread_id, identity = _selected_executor("dispatch-events-off-loop")
    tool = ProbeTool()
    await execute_as(registry_of(tool), identity, thread_id, tool.name)
    db = _db()
    try:
        db.commit()
        settlement = _stopped_with_open_calls(db, "terminal-off-loop", open_calls=1)
        await drive_terminal_settlement(settlement)
    finally:
        db.close()

    assert [action for action, _thread in emit_threads] == [
        "recorded", "settled", "terminal_unknown",
    ]
    assert all(ident != loop_thread for _action, ident in emit_threads)
