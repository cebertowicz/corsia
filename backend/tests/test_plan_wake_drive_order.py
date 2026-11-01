"""A worker's Turn releases before the parent it woke is driven.

The parents a Turn end woke start only once that worker's own terminal
settlement is released: a parent's Turn claimed and dispatched while the
worker's Turn is still held would make the worker's release wait on it.
Each test records the release and the parent drive into one ordered log.
"""

from __future__ import annotations

from automation import plan_liveness_terminal
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import infra.thread_execution as thread_execution
from tests.child_parent_notice_helpers import delegated, wakes
from tests.test_plan_wake_drive import lineage  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('signed_in_defaults')


@pytest.fixture
def order(lineage, monkeypatch):
    """One ordered log of every parent drive; the seams append their release."""
    events: list[str] = []

    async def drive(thread_id: str) -> None:
        events.append(f"drive:{thread_id}")

    monkeypatch.setattr(thread_execution, "drive_thread", drive)
    return events


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        ({"kind": "turn_failed", "cause": "process_exited"}, "child_turn_errored"),
        ({"kind": "result", "is_error": True}, "child_turn_errored"),
        ({"kind": "result", "is_error": False}, "child_returned_nothing"),
    ],
)
def test_a_codex_childs_parent_is_driven_after_its_terminal_release(
    lineage, order, monkeypatch, event, kind,
):
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime, codex_runtime_events
    from tests.test_external_harness_plan_liveness import (
        _bind_active_turn,
        _deliver_codex_terminal,
    )

    _reset_runtime()
    delegated(lineage, with_worker_node=kind == "child_turn_errored")
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread", session=None,
        parent_thread_id="parent", name="codex",
    )
    _bind_active_turn(live, "worker")
    release = codex_runtime_events.release_and_drive_terminal

    async def recorded_release(ctx, settlement) -> None:
        order.append("release:start")
        await release(ctx, settlement)
        order.append("release:done")

    monkeypatch.setattr(codex_runtime_events, "release_and_drive_terminal", recorded_release)

    _run(_deliver_codex_terminal(
        codex_runtime._make_on_event(live, "worker", MagicMock()), event, live,
    ))

    assert len(wakes(lineage)) == 1
    assert order == ["release:start", "release:done", "drive:parent"]


def test_a_codex_release_failure_still_drives_the_parent_it_woke(
    lineage, order, monkeypatch,
):
    """The woken parent is durable work: a release that raises must not strand it."""
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime, codex_runtime_events
    from tests.test_external_harness_plan_liveness import (
        _bind_active_turn,
        _deliver_codex_terminal,
    )

    _reset_runtime()
    delegated(lineage)
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread", session=None,
        parent_thread_id="parent", name="codex",
    )
    _bind_active_turn(live, "worker")

    async def failing_release(ctx, settlement) -> None:
        order.append("release:start")
        raise RuntimeError("release failed")

    monkeypatch.setattr(codex_runtime_events, "release_and_drive_terminal", failing_release)

    with pytest.raises(RuntimeError, match="release failed"):
        _run(_deliver_codex_terminal(
            codex_runtime._make_on_event(live, "worker", MagicMock()),
            {"kind": "turn_failed", "cause": "process_exited"}, live,
        ))

    assert order == ["release:start", "drive:parent"]


async def test_cc_result_keeps_event_db_but_uses_canonical_terminal_owner(
    lineage, monkeypatch,
):
    """Result persistence keeps its caller DB while terminal settlement does not."""
    from core import turn_core
    from subagents import (
        cc_event_context,
        cc_event_result,
        cc_runtime,
        cc_stream_control,
    )
    from subagents.cc_runtime_events import _make_on_event
    from tests.test_cc_turn_preempt import _admit_active_turn

    identity = _admit_active_turn(lineage, "worker", "turn-worker")
    live = cc_runtime._Live(
        session=None, parent_thread_id="parent", name="cc",
        corsia_turn=identity, turn_active=True,
    )
    commit = AsyncMock(wraps=turn_core.commit_turn_terminal)
    marker = MagicMock(wraps=cc_event_result.insert_message)
    monkeypatch.setattr(cc_event_context, "commit_turn_terminal", commit)
    monkeypatch.setattr(cc_event_result, "insert_message", marker)
    on_event = _make_on_event(
        live, "worker", lambda _thread, _message: None, lineage,
    )

    terminal = await on_event(
        {"kind": "result", "result": "provider ended", "is_error": True},
    )

    assert isinstance(terminal, cc_stream_control.TerminalWithPlanEffects)
    assert terminal.effects.plans_changed == ()
    commit.assert_awaited_once_with(
        identity.turn_id, "error", "provider ended",
        quarantine=cc_event_context._UNSETTLED_AT_TERMINAL,
    )
    assert marker.call_args.kwargs["db"] is lineage
    row = lineage.execute(
        "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()
    assert row is not None and row["status"] == "error"


async def test_cc_non_owner_returns_no_terminal_carrier(lineage, monkeypatch):
    """A losing exact-terminal claim cannot expose effects or persist a marker."""
    from core.turn_core import TurnTerminalResult
    from infra.thread_driver import TerminalSettlement
    from subagents import cc_event_context, cc_runtime
    from subagents.cc_runtime_events import _make_on_event
    from tests.test_cc_turn_preempt import _admit_active_turn

    identity = _admit_active_turn(lineage, "worker", "turn-worker")
    live = cc_runtime._Live(
        session=None, parent_thread_id="parent", name="cc",
        corsia_turn=identity, turn_active=True,
    )
    claim = AsyncMock(return_value=TerminalSettlement(
        TurnTerminalResult.CONFLICTING_TERMINAL, "worker",
    ))
    monkeypatch.setattr(cc_event_context, "commit_turn_terminal", claim)

    terminal = await _make_on_event(
        live, "worker", lambda _thread, _message: None, lineage,
    )({"kind": "result", "result": "provider ended", "is_error": True})

    assert terminal is None
    assert live.turn_active is True
    assert lineage.execute(
        "SELECT 1 FROM messages WHERE thread_id='worker' AND role='system' "
        "AND json_extract(metadata, '$.type')='result'",
    ).fetchone() is None
    claim.assert_awaited_once_with(
        identity.turn_id, "error", "provider ended",
        quarantine=cc_event_context._UNSETTLED_AT_TERMINAL,
    )


async def test_a_claude_code_childs_parent_is_driven_after_its_settlement(
    lineage, order, monkeypatch,
):
    from core import turn_core
    from subagents import cc_runtime, cc_stream_control
    from subagents.cc_runtime_events import _make_on_event
    from tests.test_cc_turn_preempt import _admit_active_turn

    delegated(lineage)
    identity = _admit_active_turn(lineage, "worker", "turn-worker")
    live = cc_runtime._Live(
        session=None, parent_thread_id="parent", name="cc",
        corsia_turn=identity, turn_active=True,
    )

    async def drive_settlement(settlement) -> None:
        order.append("release")

    monkeypatch.setattr(turn_core, "drive_turn_settlement", drive_settlement)
    on_event = _make_on_event(live, "worker", lambda _thread, _message: None, None)

    terminal = await on_event(
        {"kind": "result", "result": "provider ended", "is_error": True},
    )

    assert len(wakes(lineage)) == 1
    assert order == []
    # The stream hands the returned terminal to its driver once the callback exits.
    session = SimpleNamespace(corsia_thread_id="worker", _proc=None, _terminal_driver=None)
    delivered = asyncio.get_running_loop().create_future()
    delivered.set_result(terminal)
    cc_stream_control.observe_terminal_event(session, delivered)
    await session._terminal_driver

    assert order == ["release", "drive:parent"]


async def test_cc_broadcast_failure_is_recoverable_after_commit(
    lineage, order, monkeypatch, caplog,
):
    """A real invalidation failure cannot kill CC or strand committed work."""
    from core import turn_core
    from plans import notifications, service
    from subagents import cc_runtime, cc_stream_control
    from subagents.cc_runtime_events import _make_on_event
    from tests.plan_review_helpers import submit_review_evidence
    from tests.test_cc_turn_preempt import _admit_active_turn

    _step, _plan, node = delegated(lineage)
    service.transition_node(node.id, "active", actor_id="worker", db=lineage)
    submit_review_evidence(node.id, db=lineage, actor_id="worker")
    identity = _admit_active_turn(lineage, "worker", "turn-worker")
    lineage.execute(
        "UPDATE active_turns SET started_at=? WHERE id=?",
        ("2026-09-22T00:00:00+00:00", identity.turn_id),
    )
    lineage.commit()
    live = cc_runtime._Live(
        session=None, parent_thread_id="parent", name="cc",
        corsia_turn=identity, turn_active=True,
    )

    async def drive_settlement(_settlement) -> None:
        order.append("release")

    def failing_broadcast(_user_id, _message) -> None:
        raise RuntimeError("broadcast down")

    caplog.set_level(logging.WARNING, logger="plans.notifications")
    monkeypatch.setattr(turn_core, "drive_turn_settlement", drive_settlement)
    monkeypatch.setattr(notifications.state, "broadcast_to_user", failing_broadcast)
    terminal = await _make_on_event(
        live, "worker", lambda _thread, _message: None, None,
    )({"kind": "result", "result": "provider ended", "is_error": True})
    process = SimpleNamespace(returncode=None, kill=MagicMock())
    session = SimpleNamespace(
        corsia_thread_id="worker", _proc=process, _terminal_driver=None,
    )
    completed = asyncio.get_running_loop().create_future()
    completed.set_result(terminal)

    cc_stream_control.observe_terminal_event(session, completed)
    await session._terminal_driver

    # The errored worker Turn took in an input it never answered, so it is also woken.
    assert order == ["release", "drive:parent", "drive:worker"]
    process.kill.assert_not_called()
    assert any(
        "Post-commit plans_changed delivery failed" in message
        for message in caplog.messages
    )


def _native_handler():
    from core import step_handler as step_module
    from core.turn_core import TurnIdentity

    handler = step_module.StepHandler(
        "worker", {"model": "test/model", "model_name": "Test"},
        AsyncMock(), "turn-worker",
    )
    handler.bind_turn_identity(TurnIdentity("turn-worker", 1))
    return handler


async def test_a_native_childs_parent_is_driven_after_its_terminal_release(
    lineage, order, monkeypatch,
):
    """The native seam already publishes its plan write after the settlement drive."""
    from core import native_turn_runtime
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from infra import state
    from infra.thread_driver import TerminalSettlement

    delegated(lineage, with_worker_node=False)
    identity = TurnIdentity("turn-worker", 1)

    async def run_turn_with_steps(*_args, on_step, **_kwargs):
        await on_step("turn_complete", {})

    async def commit_terminal(*_args, quarantine):
        return TerminalSettlement(TurnTerminalResult.APPLIED, "worker")

    async def drive_settlement(_settlement) -> None:
        order.append("release")

    monkeypatch.setattr(native_turn_runtime, "run_turn_with_steps", run_turn_with_steps)
    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", commit_terminal)
    monkeypatch.setattr(native_turn_runtime, "drive_turn_settlement", drive_settlement)
    monkeypatch.setattr(state, "_active_turns", {})
    monkeypatch.setattr(state, "_active_turn_tasks", {})

    await native_turn_runtime.run_tracked_turn(native_turn_runtime.NativeTurn(
        agent=SimpleNamespace(), thread_id="worker", content="run",
        model="test/model", turn_provider={}, handler=_native_handler(),
        cancel_event=asyncio.Event(), identity=identity,
    ))

    assert len(wakes(lineage)) == 1
    assert order == ["release", "drive:parent"]


@pytest.fixture
def plan_log(monkeypatch):
    """Capture what the native seam logs about its plan write, by message."""
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(lambda record: messages.append(str(record)), level="WARNING")
    yield messages
    logger.remove(sink)


async def test_a_failed_plan_write_and_a_failed_drive_log_as_different_failures(
    lineage, monkeypatch, plan_log,
):
    from automation import plan_liveness
    from plans import public

    delegated(lineage, with_worker_node=False)
    handler = _native_handler()

    async def failing_drive(_threads) -> None:
        raise RuntimeError("drive down")

    monkeypatch.setattr(public, "drive_threads", failing_drive)
    await handler.handle("turn_complete", {})

    assert len(wakes(lineage)) == 1
    assert any("parent drive after turn completion failed: drive down" in m for m in plan_log)
    assert not any("plan-liveness arm" in m for m in plan_log)

    plan_log.clear()

    def failing_write(*_args, **_kwargs):
        raise RuntimeError("write down")

    monkeypatch.setattr(plan_liveness_terminal, "on_thread_turn_complete", failing_write)
    await handler.handle("turn_complete", {})

    assert any("plan-liveness arm after turn completion failed: write down" in m for m in plan_log)
    assert not any("parent drive" in m for m in plan_log)


async def test_failed_invalidation_broadcast_does_not_strand_the_parent(
    lineage, order, monkeypatch, caplog,
):
    """A committed plan write still drives after its socket nudge fails."""
    from plans import notifications, service
    from tests.plan_review_helpers import submit_review_evidence

    _step, _plan, node = delegated(lineage)
    service.transition_node(node.id, "active", actor_id="worker", db=lineage)
    submit_review_evidence(node.id, db=lineage, actor_id="worker")
    handler = _native_handler()

    def failing_broadcast(_user_id, _message) -> None:
        raise RuntimeError("broadcast down")

    caplog.set_level(logging.WARNING, logger="plans.notifications")
    monkeypatch.setattr(notifications.state, "broadcast_to_user", failing_broadcast)

    await handler.handle("turn_complete", {})

    assert order == ["drive:parent"]
    assert any(
        "Post-commit plans_changed delivery failed" in message
        for message in caplog.messages
    )


async def test_a_claude_code_process_exit_now_drives_the_parent_it_woke(lineage, order) -> None:
    """A worker crash never reached plan liveness; its exit terminal now records the Turn."""
    from core.turn_core import drive_turn_settlement
    from subagents import cc_runtime as rt
    from tests.test_cc_turn_preempt import _admit_active_turn

    delegated(lineage)
    identity = _admit_active_turn(lineage, "worker", "turn-worker")
    live = rt._Live(session=None, parent_thread_id="parent", name="cc",
                    corsia_turn=identity, turn_active=True)

    settlement = await rt._finish_process_exit(
        "worker", live, 1, "provider died", lambda *_a: None, None, defer_drive=True,
    )

    assert len(wakes(lineage)) == 1
    assert order == []
    await drive_turn_settlement(settlement)
    assert "drive:parent" in order
