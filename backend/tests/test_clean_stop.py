"""Exact emergency-Stop contracts for Native, Claude Code, and Codex."""

from __future__ import annotations

from llm.context_window import WindowFacts
import asyncio
from types import SimpleNamespace
from typing import Literal

import pytest
from core.continuity_accounting import ContinuityAccounting

from external_harness_usage_helpers import persist_turn


class _Registry:
    def __init__(self, live) -> None:
        self.live = live

    def get(self, _thread_id):
        return self.live


class _HangingStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        await asyncio.Future()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed.set()


def _pending_switch_turn(
    thread_id: str,
    *,
    source_model: str,
    destination_model: str,
):
    """Create an active source Turn with one destination-bound FIFO input."""
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?, 'user-1', ?, 't0', 't0')",
            (thread_id, source_model),
        )
        db.commit()
        admission = admit_input(
            thread_id,
            role="user",
            content="source work",
            intent="apply_now",
            message_id=f"{thread_id}:source-input",
            turn_id=f"{thread_id}:source-turn",
            boot_epoch=BOOT_EPOCH,
            admitted_at="t1",
            db=db,
        )
        assert admission.batch is not None
        request = request_switch(
            thread_id,
            destination_model,
            operation_id=f"{thread_id}:switch",
            requested_at="t2",
            db=db,
        )
        assert request is not None and request.waiting_for_source
        accepted = accept_thread_input(
            thread_id,
            role="user",
            content="destination work",
            intent="apply_now",
            message_id=f"{thread_id}:destination-input",
            created_at="t3",
            db=db,
        )
        assert accepted.target_model == destination_model
        return admission.batch.identity
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_transport_cancellation_closes_provider_stream() -> None:
    """Cancelling a Native provider task closes its oMLX HTTP stream."""
    from llm.base_streaming import streaming_completion

    stream = _HangingStream()

    async def _completion(**_kwargs):
        return stream

    task = asyncio.create_task(streaming_completion(
        {},
        "Fortissimo",
        None,
        None,
        acompletion_fn=_completion,
    ))
    await stream.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_native_stream_accepts_input_before_waiting_for_first_delta() -> None:
    """Provider stream acquisition is the delivery boundary, not first text."""
    from llm.base_streaming import streaming_completion

    order: list[str] = []

    async def _terminal_only_stream():
        """Only the provider's finish marker: acceptance never waits on text."""
        order.append("first_delta_wait")
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=None, finish_reason="stop")],
            usage=None,
        )

    async def _completion(**_kwargs):
        order.append("stream_open")
        return _terminal_only_stream()

    async def _accepted():
        order.append("input_accepted")

    await streaming_completion(
        {},
        "Fortissimo",
        None,
        None,
        on_response_started=_accepted,
        acompletion_fn=_completion,
    )

    assert order == ["stream_open", "input_accepted", "first_delta_wait"]


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_id", ("checkpoint-native", None))
async def test_native_stop_signals_exact_waiter_without_cancelling_checkpoint(
    monkeypatch,
    checkpoint_id,
) -> None:
    """Native Stop cancels provider I/O and exact foreground work immediately."""
    from core.native_turn_control import (
        TurnCancelEvent,
        cancel_and_join_native_turn,
    )
    from core import checkpoint_runtime
    from core.turn_core import TurnIdentity
    from host_access.processes import PROCESS_OWNER
    from infra import state

    thread_id = "native-stop"
    identity = TurnIdentity("turn-native", 1)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)
    provider_started = asyncio.Event()
    provider_stopped = asyncio.Event()
    process_stops = []
    checkpoint_stops = []
    cancel_event.checkpoint_wait_scope.checkpoint_id = checkpoint_id

    async def _provider() -> None:
        provider_started.set()
        try:
            await asyncio.Future()
        finally:
            provider_stopped.set()

    async def _owner(provider_task) -> None:
        try:
            await provider_task
        except asyncio.CancelledError:
            pass
        finally:
            if not cancel_event.completion.done():
                cancel_event.completion.set_result(True)

    async def _terminate(thread_id, turn_id, **kwargs):
        process_stops.append((thread_id, turn_id, kwargs))

    async def _cancel_checkpoint(exact_checkpoint_id):
        checkpoint_stops.append(exact_checkpoint_id)
        raise RuntimeError("minimum meaningful cold checkpoint input does not fit")

    provider_task = asyncio.create_task(_provider())
    owner_task = asyncio.create_task(_owner(provider_task))
    state._active_turns[thread_id] = cancel_event
    state._active_llm_tasks[thread_id] = provider_task
    state._active_turn_tasks[thread_id] = owner_task
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", _terminate)
    monkeypatch.setattr(checkpoint_runtime, "cancel_checkpoint", _cancel_checkpoint)
    try:
        await provider_started.wait()
        assert await cancel_and_join_native_turn(
            thread_id,
            expected_turn=identity,
            replacement_requested=False,
        ) is True
        assert cancel_event.is_set()
        assert provider_stopped.is_set()
        assert process_stops == [(
            thread_id,
            identity.turn_id,
            {"foreground_only": True, "reason": "native_turn_interrupted"},
        )]
        assert cancel_event.checkpoint_wait_scope.stop_requested is True
        assert checkpoint_stops == []
    finally:
        for task in (provider_task, owner_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(provider_task, owner_task, return_exceptions=True)
        state._active_turns.pop(thread_id, None)
        state._active_llm_tasks.pop(thread_id, None)
        state._active_turn_tasks.pop(thread_id, None)


@pytest.mark.asyncio
async def test_cc_stop_reaches_process_kill_boundary(monkeypatch) -> None:
    """Claude Code Stop kills its exact CLI and foreground process group."""
    from core.turn_core import TurnIdentity
    from host_access.processes import PROCESS_OWNER
    import subagents.cc_runtime_quiescence as quiescence

    calls = []

    class Session:
        mcp_config = None

        def _request_terminate(self) -> None:
            calls.append("request_terminate")

        async def terminate(self) -> None:
            calls.append("terminate")

    live = SimpleNamespace(
        corsia_turn=TurnIdentity("turn-cc", 1),
        provider_context_untrusted=False,
        tool_token="",
        tool_config="",
        tool_registry=None,
        admitted_tool_tasks=set(),
        session=Session(),
    )

    async def _terminate_processes(*_args, **_kwargs):
        calls.append("process_owner")

    monkeypatch.setattr(quiescence, "_LIVE", _Registry(live))
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", _terminate_processes)

    assert await quiescence.halt_exact_live_before_transition(
        "thread-cc", "turn-cc",
    ) is True
    assert calls == ["request_terminate", "process_owner", "terminate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_id", ("checkpoint-cc", None))
async def test_cc_stop_signals_its_exact_hard_wait_before_handle_stop(
    monkeypatch,
    checkpoint_id,
) -> None:
    from core.checkpoint_resume import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import cc_stop

    calls = []
    scope = CheckpointWaitScope(checkpoint_id)
    live = SimpleNamespace(
        corsia_turn=TurnIdentity("turn-cc", 1),
        checkpoint_wait_scope=scope,
        turn_active=True,
        turn_terminal=asyncio.Event(),
        interrupt_requested=False,
        apply_now_interrupt_turn=None,
    )

    class Registry:
        def get(self, _thread_id):
            return live

        def existing_lock(self, _thread_id):
            return None

    async def cancel(exact_checkpoint_id):
        calls.append(("checkpoint", exact_checkpoint_id))
        raise RuntimeError("minimum meaningful cold checkpoint input does not fit")

    async def halt(*_args):
        calls.append(("halt", "turn-cc"))
        return True

    async def close(*_args, **_kwargs):
        calls.append(("close", "turn-cc"))
        return True, None

    monkeypatch.setattr(cc_stop, "_LIVE", Registry())
    monkeypatch.setattr("core.checkpoint_runtime.cancel_checkpoint", cancel)
    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt)
    monkeypatch.setattr(cc_stop, "_close_live_for_stop", close)

    assert await cc_stop.stop(
        "thread-cc",
        expected_turn_id="turn-cc",
    ) is True
    assert scope.stop_requested is True
    assert calls == [("halt", "turn-cc"), ("close", "turn-cc")]


@pytest.mark.asyncio
async def test_cc_stale_stop_cannot_poison_successor_bound_under_transition_lock(
    monkeypatch,
) -> None:
    """An inactive predecessor cannot kill or lend its Stop scope to a successor."""
    from core.turn_core import TurnIdentity
    from subagents import cc_accept, cc_runtime, cc_stop

    thread_id = "cc-stale-stop-during-successor-bind"
    predecessor = TurnIdentity("turn-cc-predecessor", 1)
    successor = TurnIdentity("turn-cc-successor", 2)
    persist_turn(thread_id, successor.turn_id)
    live = cc_runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="cc",
        corsia_turn=predecessor,
        turn_active=False,
    )
    predecessor_scope = live.checkpoint_wait_scope
    successor_bound = asyncio.Event()
    halt_calls: list[str] = []

    async def halt(*_args) -> bool:
        halt_calls.append(predecessor.turn_id)
        await successor_bound.wait()
        return False

    async def bind_successor() -> None:
        await asyncio.sleep(0)
        admitted = await cc_accept._admit_turn(
            thread_id,
            live,
            SimpleNamespace(),
            successor.turn_id,
            successor,
        )
        assert admitted == successor
        successor_bound.set()

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt)
    stop_task = asyncio.create_task(cc_stop.stop(
        thread_id,
        expected_turn_id=predecessor.turn_id,
    ))
    bind_task = asyncio.create_task(bind_successor())
    try:
        assert await asyncio.wait_for(stop_task, timeout=0.5) is False
        await asyncio.wait_for(bind_task, timeout=0.5)
        assert halt_calls == []
        assert predecessor_scope.stop_requested is False
        assert live.corsia_turn == successor
        assert live.turn_active is True
        assert isinstance(live.turn_terminal, asyncio.Event)
        assert live.checkpoint_wait_scope is not predecessor_scope
        assert live.checkpoint_wait_scope.stop_requested is False
    finally:
        successor_bound.set()
        transition_lock.release()
        await asyncio.gather(stop_task, bind_task, return_exceptions=True)
        cc_runtime._LIVE.reset()


async def _wait_for_successor_drive(driven_settlements, successor_driven):
    """Wait for Stop's background drive; only one drive may carry a successor."""
    await asyncio.wait_for(successor_driven.wait(), timeout=0.5)
    for _ in range(20):
        successors = [
            settlement for _held, settlement in driven_settlements
            if settlement.successor is not None
        ]
        if successors:
            return successors
        await asyncio.sleep(0.01)
    return []


@pytest.mark.parametrize(
    "settlement_failure",
    ("none", "publication", "persistence"),
)
@pytest.mark.parametrize("boundary_error", (False, True))
@pytest.mark.asyncio
async def test_cc_checkpoint_stop_uses_close_as_single_terminal_owner(
    isolated_db,
    monkeypatch,
    settlement_failure: str,
    boundary_error: bool,
) -> None:
    """A released checkpoint waiter leaves the ordinary close path terminal owner."""
    from core.checkpoint_wait import await_checkpoint_or_stop
    from core.turn_core import BOOT_EPOCH
    from infra import state
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.input_render import PreparedBatch
    from infra.thread_driver import admit_input
    from subagents import cc_accept, cc_runtime, cc_stop
    from ws.stop_control import do_stop
    import infra.send_dispatch as send_dispatch
    import infra.thread_execution as execution

    thread_id = "cc-stop-checkpoint-boundary"
    turn_id = "turn-cc-checkpoint-boundary"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?, 'user-1', 'claude-code/sonnet', 't0', 't0')",
            (thread_id,),
        )
        db.commit()
        thread_row = dict(db.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone())
        admission = admit_input(
            thread_id,
            role="user",
            content="continue after checkpoint",
            intent="apply_now",
            message_id="input-cc-checkpoint-boundary",
            turn_id=turn_id,
            boot_epoch=BOOT_EPOCH,
            admitted_at="t1",
            db=db,
        )
        assert admission.batch is not None
        batch = admission.batch
        queued = accept_thread_input(
            thread_id,
            role="user",
            content="queued successor",
            intent="apply_now",
            message_id="input-cc-checkpoint-successor",
            created_at="t1.5",
            db=db,
        )
        assert queued.message_id == "input-cc-checkpoint-successor"
    finally:
        db.close()

    target = SimpleNamespace(harness="claude-code")
    prepared = PreparedBatch(
        thread_row=thread_row,
        target=target,
        provider="cc",
        is_subagent=False,
        content="continue after checkpoint",
        agent_envelope=None,
        attachments=[],
        media_projection=None,
        prepared_context=None,
        refresh_prepared_context=None,
        runtime_truth={},
    )
    checkpoint_waiting = asyncio.Event()
    halt_waiting = asyncio.Event()
    release_halt = asyncio.Event()
    dispatch_finished = asyncio.Event()
    successor_driven = asyncio.Event()
    driven_settlements = []
    outer_settlement_calls = []
    broadcasts = []

    class Session:
        mcp_config = None

        async def close(self) -> None:
            return None

    live = cc_runtime._Live(
        session=Session(),
        parent_thread_id=None,
        name="cc",
        model="sonnet",
        catalog_model_id="claude-code/sonnet",
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)
    original_broadcast_batch = execution._broadcast_batch
    batch_broadcast_count = 0

    async def wait_for_checkpoint(_checkpoint_id: str):
        checkpoint_waiting.set()
        await asyncio.Future()

    async def initial_boundary(current, identity):
        assert current is live
        assert identity == batch.identity
        assert current.turn_active is True
        assert isinstance(current.turn_terminal, asyncio.Event)
        try:
            await await_checkpoint_or_stop(
                current.checkpoint_wait_scope,
                "checkpoint-cc-boundary",
                wait_for_checkpoint,
            )
        except asyncio.CancelledError:
            if boundary_error:
                raise RuntimeError("checkpoint cleanup failed after Stop")
            raise
        raise AssertionError("checkpoint wait should have been stopped")

    async def dispatch_cc(_provider, ctx):
        try:
            async with cc_runtime._LIVE.lock(thread_id):
                identity = await cc_accept.accept_reconstructed_input(
                    thread_id,
                    live,
                    SimpleNamespace(turn_text=ctx.content),
                    turn_id=turn_id,
                    bcast=lambda _thread_id, payload: broadcasts.append(payload),
                    mark_accepted=ctx.mark_accepted,
                    mark_ambiguous=ctx.mark_ambiguous,
                    settle_delivery=ctx.settle_delivery,
                    transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
                    settle_delivery_terminal=ctx.settle_delivery_terminal,
                    preadmitted_turn=ctx.preadmitted_turn,
                    before_provider_write=initial_boundary,
                )
            return "cc_runtime.feed", None, identity
        finally:
            dispatch_finished.set()

    async def reject_outer_settlement(*args, **kwargs):
        outer_settlement_calls.append((args, kwargs))
        raise AssertionError("CC Stop terminal returned to the outer fence owner")

    async def reject_acceptance_terminal(*_args, **_kwargs):
        raise AssertionError("CC acceptance became the Stop terminal owner")

    async def maybe_fail_terminal_publication(current_batch) -> None:
        nonlocal batch_broadcast_count
        batch_broadcast_count += 1
        if settlement_failure == "publication" and batch_broadcast_count == 2:
            raise RuntimeError("terminal input publication failed")
        await original_broadcast_batch(current_batch)

    original_run_in_db = execution.run_in_db

    async def maybe_fail_input_settlement(action, *args, **kwargs):
        if (
            settlement_failure == "persistence"
            and action is execution.settle_batch_write
        ):
            raise RuntimeError("input settlement persistence failed")
        return await original_run_in_db(action, *args, **kwargs)

    async def halt_exact(*_args) -> bool:
        assert live.turn_active is True
        assert live.checkpoint_wait_scope.stop_requested is True
        halt_waiting.set()
        await release_halt.wait()
        return True

    async def owns_thread(*_args) -> bool:
        return True

    async def prepare(*_args, **_kwargs):
        return prepared

    async def drive_terminal_settlement(settlement) -> None:
        assert cc_runtime._LIVE.get(thread_id) is None
        driven_settlements.append((transition_lock.locked(), settlement))
        successor_driven.set()

    monkeypatch.setattr(send_dispatch, "dispatch", dispatch_cc)
    monkeypatch.setattr(
        state,
        "owner_broadcast",
        lambda _user_id: lambda _thread_id, payload: broadcasts.append(payload),
    )
    monkeypatch.setattr(
        state,
        "broadcast_event",
        lambda _thread_id, _user_id, payload: broadcasts.append(payload),
    )
    monkeypatch.setattr(
        cc_accept, "_report_prewrite_failure", reject_acceptance_terminal,
    )
    monkeypatch.setattr(
        "infra.input_writes._settle_evidence", reject_outer_settlement,
    )
    monkeypatch.setattr(execution, "run_in_db", maybe_fail_input_settlement)
    monkeypatch.setattr(execution, "_broadcast_batch", maybe_fail_terminal_publication)
    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt_exact)
    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(
        execution, "drive_terminal_settlement", drive_terminal_settlement,
    )

    dispatch_task = asyncio.create_task(execution.execute_claimed_batch(
        batch,
        thread_row=thread_row,
        target=target,
    ))
    stop_task = None
    try:
        await asyncio.wait_for(checkpoint_waiting.wait(), timeout=0.5)
        stop_task = asyncio.create_task(do_stop(
            {"id": "user-1"},
            thread_id,
            None,
            None,
            turn_id,
            owns_thread_fn=owns_thread,
        ))
        await asyncio.wait_for(halt_waiting.wait(), timeout=0.5)
        await asyncio.wait_for(dispatch_finished.wait(), timeout=0.5)
        expected_dispatch_error = (
            ("checkpoint cleanup failed after Stop" if boundary_error else None)
            if settlement_failure == "none"
            else (
                "terminal input publication failed"
                if settlement_failure == "publication"
                else "input settlement persistence failed"
            )
        )
        if expected_dispatch_error is None:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(dispatch_task), timeout=0.5)
        else:
            with pytest.raises(RuntimeError, match=expected_dispatch_error):
                await asyncio.wait_for(asyncio.shield(dispatch_task), timeout=0.5)
        assert outer_settlement_calls == []
        assert live.turn_active is True
        assert driven_settlements == []
        check = get_db()
        try:
            assert check.execute(
                "SELECT status FROM active_turns WHERE id=?", (turn_id,),
            ).fetchone()["status"] == "running"
            input_row = check.execute(
                "SELECT delivery_state,write_outcome FROM messages WHERE id=?",
                ("input-cc-checkpoint-boundary",),
            ).fetchone()
            if settlement_failure == "persistence":
                assert tuple(input_row) == ("queued", None)
            else:
                assert tuple(input_row) == ("cancelled", "proved_no_write")
        finally:
            check.close()
        release_halt.set()

        # A fenced write whose settlement failed has no provable outcome, so
        # Stop ends the Turn itself and records that input ambiguous.
        expected_input = (
            ("ambiguous", "ambiguous") if settlement_failure == "persistence"
            else ("cancelled", "proved_no_write")
        )
        receipt = await asyncio.wait_for(stop_task, timeout=0.5)
        assert receipt.outcome == "stopped"
        successor_settlements = await _wait_for_successor_drive(
            driven_settlements, successor_driven,
        )
        assert all(lock_held is False for lock_held, _ in driven_settlements)
        assert len(successor_settlements) == 1
        assert tuple(
            item.message_id for item in successor_settlements[0].successor.items
        ) == ("input-cc-checkpoint-successor",)
        assert any(
            event["type"] == "turn_cancelled" for event in broadcasts
        )
        check = get_db()
        try:
            assert check.execute(
                "SELECT status FROM active_turns WHERE id=?", (turn_id,),
            ).fetchone()["status"] == "cancelled"
            assert tuple(check.execute(
                "SELECT delivery_state,write_outcome FROM messages WHERE id=?",
                ("input-cc-checkpoint-boundary",),
            ).fetchone()) == expected_input
        finally:
            check.close()
    finally:
        release_halt.set()
        tasks = [dispatch_task, *([stop_task] if stop_task is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_preadmitted_stop_latch_settles_before_frame_preparation(
    monkeypatch,
) -> None:
    """A pre-write Stop settles delivery without taking terminal ownership."""
    from core.turn_core import TurnIdentity
    from subagents import cc_accept, cc_runtime
    from subagents.cc_runtime_state import activate_bound_turn

    thread_id = "cc-preadmitted-stop-feed-fence"
    identity = TurnIdentity("turn-cc-preadmitted-stop-feed-fence", 1)
    persist_turn(thread_id, identity.turn_id)
    provider_writes: list[str] = []
    delivery_events: list[str] = []
    prewrite_reports: list[BaseException] = []

    class RaisingSnapshot:
        def to_dict(self) -> dict:
            raise AssertionError("stopped input reached frame preparation")

    class Session:
        reconstructed_transcript_path = None

        async def feed(
            self,
            text: str,
            *,
            on_write_observed,
            on_accepted,
            **_kwargs,
        ) -> None:
            provider_writes.append(text)
            on_write_observed()
            on_accepted()

    live = cc_runtime._Live(
        session=Session(),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
    )
    assert activate_bound_turn(live, identity)
    assert isinstance(live.turn_terminal, asyncio.Event)
    turn_terminal = live.turn_terminal
    live.checkpoint_wait_scope.request_stop()
    frame = SimpleNamespace(
        turn_text="stopped before provider write",
        snapshot_id="snapshot-preadmitted-stop-feed-fence",
        continuity_accounting=ContinuityAccounting.not_delivered(),
        snapshot=RaisingSnapshot(),
    )

    def transfer_delivery_to_turn() -> None:
        delivery_events.append("transferred")

    async def settle_delivery_terminal(
        outcome: str,
        _error: str | None,
    ) -> None:
        delivery_events.append(outcome)

    async def report_prewrite_failure(
        _thread_id,
        _live,
        _identity,
        error,
        _bcast,
    ) -> None:
        prewrite_reports.append(error)

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    monkeypatch.setattr(cc_accept, "_report_prewrite_failure", report_prewrite_failure)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cc_accept.accept_reconstructed_input(
                thread_id,
                live,
                frame,
                turn_id=identity.turn_id,
                bcast=lambda *_args: None,
                preadmitted_turn=identity,
                transfer_delivery_to_turn=transfer_delivery_to_turn,
                settle_delivery_terminal=settle_delivery_terminal,
            )
        assert provider_writes == []
        assert delivery_events == ["transferred", "cancelled"]
        assert prewrite_reports == []
        assert cc_runtime._LIVE.get(thread_id) is live
        assert live.turn_active is True
        assert live.turn_terminal is turn_terminal
        assert turn_terminal.is_set() is False
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_follows_replacement_returned_after_wait_boundary(
) -> None:
    """Stop follows a same-Turn replacement returned after the wait clears."""
    from core.turn_core import TurnIdentity
    from subagents import cc_accept, cc_runtime
    from subagents.cc_runtime_state import activate_bound_turn

    thread_id = "cc-stop-post-wait-replacement"
    identity = TurnIdentity("turn-cc-stop-post-wait-replacement", 1)
    persist_turn(thread_id, identity.turn_id)
    frame = SimpleNamespace(
        turn_text="continue after checkpoint",
        continuity_accounting=ContinuityAccounting.not_delivered(),
    )
    delivery_events = []

    class Session:
        async def feed(self, *_args, **_kwargs) -> None:
            raise AssertionError("stopped input reached the provider")

    original = cc_runtime._Live(
        session=Session(), parent_thread_id=None, name="original",
    )
    replacement = cc_runtime._Live(
        session=Session(), parent_thread_id=None, name="replacement",
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, original)

    async def boundary(current, exact_identity):
        assert current is original
        assert exact_identity == identity
        assert current.checkpoint_wait_scope.checkpoint_id is None
        current.checkpoint_wait_scope.request_stop()
        original.corsia_turn = None
        replacement.corsia_turn = identity
        replacement.checkpoint_wait_scope = current.checkpoint_wait_scope
        assert activate_bound_turn(replacement, identity)
        cc_runtime._LIVE.put(thread_id, replacement)
        return replacement, frame

    def transfer_delivery() -> None:
        assert original.turn_active is True
        assert original.corsia_turn == identity
        assert replacement.turn_active is False
        delivery_events.append("transferred")

    async def settle_delivery(outcome: str, _error: str | None) -> None:
        assert outcome == "cancelled"
        assert replacement.turn_active is True
        delivery_events.append("settled")

    try:
        with pytest.raises(asyncio.CancelledError):
            await cc_accept.accept_reconstructed_input(
                thread_id,
                original,
                frame,
                turn_id=identity.turn_id,
                bcast=lambda *_args: None,
                preadmitted_turn=identity,
                transfer_delivery_to_turn=transfer_delivery,
                settle_delivery_terminal=settle_delivery,
                before_provider_write=boundary,
            )
        assert delivery_events == ["transferred", "settled"]
        assert replacement.checkpoint_wait_scope is original.checkpoint_wait_scope
        assert replacement.checkpoint_wait_scope.stop_requested is True
        assert replacement.turn_active is True
        assert isinstance(replacement.turn_terminal, asyncio.Event)
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.parametrize("delivery_case", ("settled", "mid_turn", "failed"))
@pytest.mark.asyncio
async def test_cc_stop_fences_reconstructed_feed_queued_on_event_lock(
    monkeypatch,
    delivery_case: str,
) -> None:
    """A Stop latched behind CC's event lock cancels before provider feed."""
    from core.turn_core import TurnIdentity
    from subagents import cc_accept, cc_runtime, cc_stop

    thread_id = f"cc-stop-queued-reconstructed-feed-{delivery_case}"
    identity = TurnIdentity(f"turn-cc-queued-feed-{delivery_case}", 1)
    persist_turn(thread_id, identity.turn_id)
    lock_waiting = asyncio.Event()
    halt_waiting = asyncio.Event()
    release_halt = asyncio.Event()
    feeds = []
    delivery_events = []
    stop_terminals = []
    settlement_error = RuntimeError("CC proved-no-write settlement failed")

    class ObservedLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            if self.locked():
                lock_waiting.set()
            return await super().acquire()

    class Session:
        reconstructed_transcript_path = None

        def __init__(self) -> None:
            self._closing = False

        def _request_terminate(self) -> None:
            self._closing = True

        async def feed(self, text, *, on_write_observed, on_accepted, **_kwargs):
            feeds.append(text)
            on_write_observed()
            on_accepted()

    session = Session()
    event_lock = ObservedLock()
    live = cc_runtime._Live(
        session=session,
        parent_thread_id=None,
        name="cc",
        event_lock=event_lock,
    )
    frame = SimpleNamespace(
        turn_text="continue after checkpoint",
        snapshot_id="snapshot-cc-stop-feed",
        continuity_accounting=ContinuityAccounting.not_delivered(),
        snapshot=SimpleNamespace(to_dict=lambda: {}),
    )

    async def halt_exact(*_args) -> bool:
        assert live.checkpoint_wait_scope.stop_requested is True
        session._request_terminate()
        halt_waiting.set()
        await release_halt.wait()
        return True

    async def close_for_stop(*_args, **_kwargs):
        assert live.turn_active is True
        stop_terminals.append(True)
        return True, None

    async def reject_acceptance_terminal(*_args, **_kwargs) -> None:
        raise AssertionError("CC acceptance replaced exact Stop terminal ownership")

    def transfer_delivery_to_turn() -> None:
        assert event_lock.locked()
        delivery_events.append("transferred")

    async def settle_delivery_terminal(
        outcome: str,
        _error: str | None,
    ) -> None:
        assert event_lock.locked()
        delivery_events.append(outcome)
        if delivery_case == "failed":
            raise settlement_error

    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt_exact)
    monkeypatch.setattr(cc_stop, "_close_live_for_stop", close_for_stop)
    monkeypatch.setattr(
        cc_accept, "_report_prewrite_failure", reject_acceptance_terminal,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    await event_lock.acquire()
    callbacks = (
        {
            "transfer_delivery_to_turn": transfer_delivery_to_turn,
            "settle_delivery_terminal": settle_delivery_terminal,
        }
        if delivery_case != "mid_turn" else {}
    )
    accept_task = asyncio.create_task(cc_accept.accept_reconstructed_input(
        thread_id,
        live,
        frame,
        turn_id=identity.turn_id,
        bcast=lambda *_args: None,
        preadmitted_turn=identity,
        **callbacks,
    ))
    stop_task = None
    try:
        await asyncio.wait_for(lock_waiting.wait(), timeout=0.5)
        assert live.corsia_turn == identity
        assert live.turn_active is True
        assert live.checkpoint_wait_scope.stop_requested is False

        stop_task = asyncio.create_task(cc_stop.stop(
            thread_id, expected_turn_id=identity.turn_id,
        ))
        await asyncio.wait_for(halt_waiting.wait(), timeout=0.5)
        assert live.checkpoint_wait_scope.stop_requested is True
        assert session._closing is True

        event_lock.release()
        if delivery_case == "failed":
            with pytest.raises(RuntimeError) as raised:
                await asyncio.wait_for(accept_task, timeout=0.5)
            assert raised.value is settlement_error
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(accept_task, timeout=0.5)
        assert feeds == []
        assert delivery_events == (
            [] if delivery_case == "mid_turn" else ["transferred", "cancelled"]
        )
        assert live.turn_active is True
        assert live.turn_terminal.is_set() is False
        assert cc_runtime._LIVE.get(thread_id) is live
        assert stop_terminals == []

        release_halt.set()
        assert await asyncio.wait_for(stop_task, timeout=0.5) is True
        assert stop_terminals == [True]
    finally:
        release_halt.set()
        if event_lock.locked():
            event_lock.release()
        tasks = [accept_task, *([stop_task] if stop_task is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition_locked", (True, False))
async def test_cc_idle_exit_respects_bound_transition_owner(
    monkeypatch,
    transition_locked,
) -> None:
    """Only a held transition fence prevents ordinary idle retirement."""
    from core.turn_core import TurnIdentity
    from subagents import cc_process_exit, cc_runtime

    thread_id = "cc-idle-exit-activation-race"
    identity = TurnIdentity("turn-cc-idle-exit-activation-race", 1)
    finalized = []
    live = cc_runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        turn_active=False,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)
    if transition_locked:
        await transition_lock.acquire()

    async def quiesce(*_args, **_kwargs) -> bool:
        return True

    def finalize(*_args, **_kwargs) -> None:
        finalized.append(True)
        cc_runtime._LIVE.pop(thread_id, None)

    monkeypatch.setattr(cc_process_exit, "quiesce_live_tools", quiesce)
    try:
        retired = await cc_process_exit.handle_process_exit(
            thread_id,
            live,
            1,
            "process exited before activation",
            lambda *_args: None,
            finalize_callback=finalize,
        )
        assert retired == (not transition_locked)
        assert cc_runtime._LIVE.get(thread_id) is (
            live if transition_locked else None
        )
        assert live.turn_active is False
        assert live.turn_terminal is None
        assert live.pending_process_exit is None
        assert finalized == ([] if transition_locked else [True])
    finally:
        if transition_lock.locked():
            transition_lock.release()
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_waits_on_held_lock_but_never_halts_identityless_live(
    monkeypatch,
) -> None:
    """Like Codex, Stop waits for a held lock; an unbound live is never its owner."""
    from subagents import cc_runtime, cc_stop, live_registry

    thread_id = "cc-stop-identityless-held-lock"
    live = cc_runtime._Live(
        session=SimpleNamespace(), parent_thread_id=None, name="unbound",
    )

    async def reject_halt(*_args) -> bool:
        raise AssertionError("identity-less live reached exact runtime halt")

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", reject_halt)
    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", 0.05)
    try:
        assert await asyncio.wait_for(
            cc_stop.stop(thread_id, expected_turn_id="missing-turn"),
            timeout=0.5,
        ) is False
        assert cc_runtime._LIVE.get(thread_id) is live
    finally:
        transition_lock.release()
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_settles_existing_pending_exit(
    isolated_db,
    monkeypatch,
) -> None:
    """Ordinary process-exit ownership survives a concurrent Stop request."""
    from infra.database import get_db
    from subagents import cc_runtime, cc_stop
    from subagents.cc_runtime_state import PendingProcessExit

    thread_id = "cc-stop-ordinary-pending-exit"
    identity = _pending_switch_turn(
        thread_id,
        source_model="claude-code/source",
        destination_model="claude-code/destination",
    )
    driven = []
    broadcasts = []

    class Session:
        mcp_config = None

        async def close(self) -> None:
            return None

    live = cc_runtime._Live(
        session=Session(),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        turn_active=True,
        pending_process_exit=PendingProcessExit(
            identity, 7, "natural exit before Stop terminal",
        ),
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)

    async def halt_exact(*_args) -> bool:
        return True

    async def drive(settlement) -> None:
        assert cc_runtime._LIVE.get(thread_id) is None
        assert transition_lock.locked() is False
        driven.append(settlement)

    monkeypatch.setattr(
        cc_stop, "halt_exact_live_before_transition", halt_exact,
    )
    monkeypatch.setattr(cc_stop, "drive_turn_settlement", drive)
    try:
        assert await cc_stop.stop(
            thread_id,
            expected_turn_id=identity.turn_id,
            broadcast=lambda _thread_id, payload: broadcasts.append(payload),
        ) is True
        assert live.pending_process_exit is None
        assert live.process_exit_resolved is True
        assert len(driven) == 1
        assert driven[0].switch_request is not None
        assert (
            driven[0].switch_request.destination_model
            == "claude-code/destination"
        )
        assert any(event["type"] == "turn_complete" for event in broadcasts)
        db = get_db()
        try:
            row = db.execute(
                "SELECT status,error FROM active_turns WHERE id=?",
                (identity.turn_id,),
            ).fetchone()
            assert tuple(row) == ("error", "natural exit before Stop terminal")
        finally:
            db.close()
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_rehalts_same_turn_replacement_before_transition_lock(
    monkeypatch,
) -> None:
    """A replacement published during the first halt is killed before lock wait."""
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime, cc_stop

    thread_id = "cc-stop-rehalt-same-turn-replacement"
    identity = TurnIdentity("turn-cc-rehalt-replacement", 1)
    old = cc_runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="old",
        corsia_turn=identity,
        turn_active=True,
    )
    replacement = cc_runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="replacement",
        corsia_turn=identity,
        turn_active=True,
    )
    halt_targets = []
    lock_waiting = asyncio.Event()
    closed = []

    async def halt_exact(*_args) -> bool:
        target = cc_runtime._LIVE.get(thread_id)
        halt_targets.append(target)
        if target is old:
            cc_runtime._LIVE.put(thread_id, replacement)
        return True

    original_acquire = cc_stop._acquire_stop_lock

    async def observe_lock_wait(child_thread_id, lock) -> bool:
        lock_waiting.set()
        return await original_acquire(child_thread_id, lock)

    async def close_for_stop(*_args, **_kwargs):
        assert cc_runtime._LIVE.get(thread_id) is replacement
        closed.append(replacement)
        return True, None

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, old)
    transition_lock = cc_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt_exact)
    monkeypatch.setattr(cc_stop, "_acquire_stop_lock", observe_lock_wait)
    monkeypatch.setattr(cc_stop, "_close_live_for_stop", close_for_stop)
    stop_task = asyncio.create_task(cc_stop.stop(
        thread_id, expected_turn_id=identity.turn_id,
    ))
    try:
        await asyncio.wait_for(lock_waiting.wait(), timeout=0.5)
        assert halt_targets == [old, replacement]
        assert old.checkpoint_wait_scope.stop_requested is True
        assert replacement.checkpoint_wait_scope.stop_requested is True
        assert stop_task.done() is False

        transition_lock.release()
        assert await asyncio.wait_for(stop_task, timeout=0.5) is True
        assert closed == [replacement]
    finally:
        if transition_lock.locked():
            transition_lock.release()
        await asyncio.gather(stop_task, return_exceptions=True)
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stopped_continuation_never_builds_replacement_process(
    monkeypatch,
) -> None:
    """An inherited Stop latch aborts CC replacement before session creation."""
    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime_helpers, cc_startup_process
    from subagents.cc_runtime_state import _LIVE, _Live

    thread_id = "cc-stopped-continuation-pre-spawn"
    identity = TurnIdentity("turn-cc-stopped-continuation", 1)
    wait_scope = CheckpointWaitScope()
    wait_scope.request_stop()
    builds = []
    starts = []
    cleaned = []

    class Session:
        async def start(self) -> None:
            starts.append(True)

    old = _Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="old",
        corsia_turn=identity,
        turn_active=True,
        checkpoint_wait_scope=wait_scope,
    )
    context = SimpleNamespace(
        row={
            "spawned_by": None,
            "title": "cc",
            "model": "claude-code/test",
            "runtime_context_epoch": 3,
            "project_id": None,
        },
        is_subagent=False,
        model_alias="test",
        target_context_window=262_144,
        target_discovered_context_window=None,
        expected_reported_model=None,
        working_dir="/tmp",
        resume_session_id="provider-session",
        cc_session_id="provider-session",
        reconstruction=SimpleNamespace(
            snapshot_id="snapshot-stopped-continuation",
            snapshot=SimpleNamespace(to_dict=lambda: {}),
        ),
        tool_registry=None,
        tool_definitions=(),
        synthetic_resume=False,
        launch_identity=object(),
    )

    def build_session(*_args, **_kwargs):
        builds.append(True)
        return Session()

    def cleanup_live(live, **_kwargs) -> None:
        cleaned.append(live)

    monkeypatch.setattr(cc_startup_process, "resolve_claude_dir", lambda _: "/tmp")
    monkeypatch.setattr(cc_startup_process, "_make_on_event", lambda *_args: None)
    monkeypatch.setattr(cc_startup_process, "build_starting_session", build_session)
    monkeypatch.setattr(
        cc_startup_process, "install_starting_identity", lambda *_args: None,
    )
    monkeypatch.setattr(cc_startup_process, "cleanup_ephemeral_live", cleanup_live)
    monkeypatch.setattr(cc_runtime_helpers, "_validate_working_dir", lambda *_: None)
    _LIVE.reset()
    _LIVE.put(thread_id, old)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cc_startup_process.launch_cc_process(
                thread_id,
                context,
                binary="claude",
                claude_dir=None,
                effort=None,
                broadcast=lambda *_args: None,
                seed_file="",
                source_artifacts=[],
                surface_exit_error_fn=lambda *_args: None,
                finalize_fn=lambda *_args, **_kwargs: None,
                db=object(),
                held_lock=asyncio.Lock(),
                expected_live=old,
                continuing_identity=identity,
                continuing_wait_scope=wait_scope,
            )
        assert builds == []
        assert starts == []
        assert len(cleaned) == 1
        assert cleaned[0] is not old
        assert cleaned[0].checkpoint_wait_scope is wait_scope
        assert _LIVE.get(thread_id) is old
        assert old.turn_active is True
    finally:
        _LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_prefers_registered_same_turn_replacement(
    isolated_db,
    monkeypatch,
) -> None:
    """A checkpoint replacement remains the exact Stop target for its Turn."""
    from infra.database import get_db
    from subagents import cc_runtime, cc_stop

    thread_id = "cc-stop-same-turn-replacement"
    identity = _pending_switch_turn(
        thread_id,
        source_model="claude-code/source",
        destination_model="claude-code/destination",
    )
    closed = []
    driven = []

    class Session:
        mcp_config = None

        def __init__(self, label: str) -> None:
            self.label = label

        async def close(self) -> None:
            closed.append(self.label)

    captured = cc_runtime._Live(
        session=Session("captured"),
        parent_thread_id=None,
        name="captured",
        corsia_turn=identity,
        turn_active=True,
    )
    replacement = cc_runtime._Live(
        session=Session("replacement"),
        parent_thread_id=None,
        name="replacement",
        corsia_turn=identity,
        turn_active=True,
        checkpoint_wait_scope=captured.checkpoint_wait_scope,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, captured)
    transition_lock = cc_runtime._LIVE.lock(thread_id)

    async def halt_then_replace(*_args) -> bool:
        assert captured.checkpoint_wait_scope.stop_requested is True
        captured.corsia_turn = None
        captured.turn_active = False
        cc_runtime._LIVE.put(thread_id, replacement)
        return True

    async def drive(settlement) -> None:
        assert cc_runtime._LIVE.get(thread_id) is None
        assert transition_lock.locked() is False
        driven.append(settlement)

    monkeypatch.setattr(
        cc_stop, "halt_exact_live_before_transition", halt_then_replace,
    )
    monkeypatch.setattr(cc_stop, "drive_turn_settlement", drive)
    try:
        assert await cc_stop.stop(
            thread_id, expected_turn_id=identity.turn_id,
        ) is True
        assert closed == ["replacement"]
        assert len(driven) == 1
        db = get_db()
        try:
            assert db.execute(
                "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
            ).fetchone()["status"] == "cancelled"
        finally:
            db.close()
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_drives_pending_switch_after_releasing_transition_lock(
    isolated_db,
    monkeypatch,
) -> None:
    """A stopped CC source cannot recursively acquire its transition lock."""
    from infra import thread_execution
    from subagents import cc_runtime, cc_stop

    thread_id = "cc-stop-pending-switch"
    source_model = "claude-code/source"
    destination_model = "claude-code/destination"
    identity = _pending_switch_turn(
        thread_id,
        source_model=source_model,
        destination_model=destination_model,
    )
    events: list[str] = []

    class Session:
        mcp_config = None

        async def close(self) -> None:
            events.append("runtime_retired")

    live = cc_runtime._Live(
        session=Session(),
        parent_thread_id=None,
        name="cc",
        model="source",
        catalog_model_id=source_model,
        corsia_turn=identity,
        turn_active=True,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)

    async def halt_exact(*_args) -> bool:
        events.append("runtime_halted")
        return True

    async def drive(settlement) -> None:
        assert settlement.switch_request is not None
        assert settlement.switch_request.destination_model == destination_model
        assert cc_runtime._LIVE.get(thread_id) is None
        assert transition_lock.locked() is False
        events.append("settlement_driven")

    monkeypatch.setattr(
        cc_stop,
        "halt_exact_live_before_transition",
        halt_exact,
    )
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    try:
        assert await asyncio.wait_for(
            cc_runtime.stop(thread_id, expected_turn_id=identity.turn_id),
            timeout=0.5,
        ) is True
        assert events == [
            "runtime_halted",
            "runtime_retired",
            "settlement_driven",
        ]
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_drives_committed_switch_before_raising_publish_error(
    isolated_db,
    monkeypatch,
) -> None:
    """A post-commit CC publish error cannot strand the committed successor."""
    from infra import thread_execution
    from subagents import cc_runtime, cc_stop

    thread_id = "cc-stop-publish-error"
    destination_model = "claude-code/destination"
    identity = _pending_switch_turn(
        thread_id,
        source_model="claude-code/source",
        destination_model=destination_model,
    )
    events: list[str] = []
    publication_error = RuntimeError("CC terminal publication failed")

    class Session:
        mcp_config = None

        async def close(self) -> None:
            events.append("runtime_retired")

    live = cc_runtime._Live(
        session=Session(), parent_thread_id=None, name="cc", model="source",
        catalog_model_id="claude-code/source", corsia_turn=identity,
        turn_active=True,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)

    async def halt_exact(*_args) -> bool:
        events.append("runtime_halted")
        return True

    def fail_terminal_publish(_thread_id, payload) -> None:
        if payload["type"] == "turn_cancelled":
            events.append("publication_failed")
            raise publication_error

    async def drive(settlement) -> None:
        assert settlement.switch_request is not None
        assert settlement.switch_request.destination_model == destination_model
        assert cc_runtime._LIVE.get(thread_id) is None
        assert transition_lock.locked() is False
        events.append("settlement_driven")

    monkeypatch.setattr(
        cc_stop, "halt_exact_live_before_transition", halt_exact,
    )
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    try:
        with pytest.raises(RuntimeError) as raised:
            await asyncio.wait_for(
                cc_runtime.stop(
                    thread_id, expected_turn_id=identity.turn_id,
                    broadcast=fail_terminal_publish,
                ),
                timeout=0.5,
            )
        assert raised.value is publication_error
        assert events == [
            "runtime_halted", "runtime_retired", "publication_failed",
            "settlement_driven",
        ]
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_finishes_after_caller_cancelled_post_halt(
    isolated_db,
    monkeypatch,
) -> None:
    """Caller cancellation cannot strand a Turn after CC has been halted."""
    from infra import thread_execution
    from subagents import cc_runtime, cc_stop

    thread_id = "cc-stop-caller-cancelled"
    identity = _pending_switch_turn(
        thread_id,
        source_model="claude-code/source",
        destination_model="claude-code/destination",
    )
    halted = asyncio.Event()
    release_halt = asyncio.Event()
    events: list[str] = []

    class Session:
        mcp_config = None

        async def close(self) -> None:
            events.append("runtime_retired")

    live = cc_runtime._Live(
        session=Session(), parent_thread_id=None, name="cc", model="source",
        catalog_model_id="claude-code/source", corsia_turn=identity,
        turn_active=True,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)

    async def halt_exact(*_args) -> bool:
        events.append("runtime_halted")
        halted.set()
        await release_halt.wait()
        return True

    async def drive(_settlement) -> None:
        assert cc_runtime._LIVE.get(thread_id) is None
        events.append("settlement_driven")

    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt_exact)
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    task = asyncio.create_task(
        cc_runtime.stop(thread_id, expected_turn_id=identity.turn_id)
    )
    try:
        await asyncio.wait_for(halted.wait(), timeout=0.5)
        task.cancel()
        release_halt.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert events == [
            "runtime_halted", "runtime_retired", "settlement_driven",
        ]
    finally:
        release_halt.set()
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_stop_drives_committed_switch_before_retirement_error(
    isolated_db,
    monkeypatch,
) -> None:
    """A failed CC close cannot discard an already-committed successor."""
    from infra import thread_execution
    from subagents import cc_runtime, cc_stop

    thread_id = "cc-stop-retirement-error"
    identity = _pending_switch_turn(
        thread_id,
        source_model="claude-code/source",
        destination_model="claude-code/destination",
    )
    retirement_error = RuntimeError("CC close failed after process death")
    events: list[str] = []

    class Session:
        mcp_config = None
        returncode = None

        async def close(self) -> None:
            events.append("retirement_failed")
            raise retirement_error

    live = cc_runtime._Live(
        session=Session(), parent_thread_id=None, name="cc", model="source",
        catalog_model_id="claude-code/source", corsia_turn=identity,
        turn_active=True,
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)

    async def halt_exact(*_args) -> bool:
        events.append("runtime_halted")
        return True

    async def ensure_terminated(_session) -> bool:
        events.append("runtime_force_retired")
        return True

    async def drive(_settlement) -> None:
        assert cc_runtime._LIVE.get(thread_id) is None
        events.append("settlement_driven")

    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt_exact)
    monkeypatch.setattr(
        "subagents.runtime_guard.ensure_terminated", ensure_terminated,
    )
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    try:
        with pytest.raises(RuntimeError) as raised:
            await cc_runtime.stop(thread_id, expected_turn_id=identity.turn_id)
        assert raised.value is retirement_error
        assert events == [
            "runtime_halted", "retirement_failed", "runtime_force_retired",
            "settlement_driven",
        ]
    finally:
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_result_publication_failure_still_drives_committed_successor(
    monkeypatch,
) -> None:
    """CC result publication cannot hide its durable terminal settlement."""
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from infra.thread_driver import TerminalSettlement
    from subagents import cc_runtime_events, cc_stream, cc_stream_control
    from subagents.cc_process_exit import _CommittedTerminalFailure
    from subagents.cc_runtime_state import _Live

    publication_error = RuntimeError("CC result publication failed")
    settlement = TerminalSettlement(TurnTerminalResult.APPLIED, "thread-cc")
    driven: list[TerminalSettlement] = []
    killed: list[bool] = []

    async def fail_after_commit(_ctx, event) -> None:
        event["_corsia_terminal_settlement"] = settlement
        raise publication_error

    async def drive(committed) -> None:
        driven.append(committed)

    def start_driver(_session, committed):
        return asyncio.create_task(drive(committed))

    monkeypatch.setattr(cc_runtime_events, "on_result", fail_after_commit)
    monkeypatch.setattr(cc_stream_control, "start_terminal_driver", start_driver)
    live = _Live(
        session=None, parent_thread_id=None, name="cc",
        corsia_turn=TurnIdentity("turn-cc", 1), turn_active=True,
    )
    on_event = cc_runtime_events._make_on_event(
        live, "thread-cc", lambda *_args: None, None,
    )
    stream = cc_stream.CCStreamSession(
        binary="claude", session_id="provider-cc",
        corsia_thread_id="thread-cc", on_event=on_event,
    )
    stream._proc = SimpleNamespace(
        returncode=None,
        kill=lambda: killed.append(True),
    )
    event = {"kind": "result"}
    task = asyncio.create_task(stream._deliver_terminal_event(None, event))
    stream._terminal_event = task
    task.add_done_callback(stream._observe_terminal_event)

    completed = await task
    await asyncio.sleep(0)
    assert isinstance(completed, _CommittedTerminalFailure)
    assert completed.publication_error is publication_error
    assert stream._terminal_driver is not None
    await stream._terminal_driver
    assert driven == [settlement]
    assert killed == []


def test_codex_stop_synchronously_requests_exact_process_death(monkeypatch) -> None:
    """Codex closes provider admission and kills before awaiting cleanup."""
    from core.turn_core import TurnIdentity
    import subagents.codex_runtime_support as support

    calls = []

    class Session:
        def _request_terminate(self) -> None:
            calls.append("request_terminate")

        def revoke_dynamic_tool_admission(self) -> None:
            calls.append("revoke_admission")

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

    session = Session()
    live = SimpleNamespace(
        corsia_turn=TurnIdentity("turn-codex", 1),
        provider_context_untrusted=False,
        session=session,
    )
    monkeypatch.setattr(support, "_LIVE", _Registry(live))

    owner = support.request_exact_stop("thread-codex", "turn-codex")

    assert owner is not None and owner[:2] == (live, session)
    assert calls == ["request_terminate", "revoke_admission"]
    assert live.provider_context_untrusted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_id", ("checkpoint-codex", None))
async def test_codex_stop_signals_its_exact_hard_wait_before_handle_stop(
    monkeypatch,
    checkpoint_id,
) -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_runtime_state import _LIVE, _LiveCodex

    calls = []
    session = SimpleNamespace()
    live = _LiveCodex(
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=TurnIdentity("turn-codex", 1),
    )
    live.checkpoint_wait_scope.checkpoint_id = checkpoint_id
    _LIVE.put("thread-codex", live)

    async def cancel(exact_checkpoint_id):
        calls.append(("checkpoint", exact_checkpoint_id))
        raise RuntimeError("minimum meaningful cold checkpoint input does not fit")

    async def terminate(*_args):
        calls.append(("terminate", "turn-codex"))
        return True

    async def fence(*_args):
        calls.append(("fence", "turn-codex"))
        return True

    async def finish(*_args, **_kwargs):
        calls.append(("finish", "turn-codex"))
        return True, None

    async def release_barrier(*_args):
        return None

    monkeypatch.setattr("core.checkpoint_runtime.cancel_checkpoint", cancel)
    monkeypatch.setattr(codex_runtime, "_is_stale_stop_live", lambda *_a: False)
    monkeypatch.setattr(
        codex_runtime,
        "_request_exact_stop",
        lambda *_a: (live, session, None),
    )
    monkeypatch.setattr(codex_runtime, "_terminate_turn_foreground_process", terminate)
    monkeypatch.setattr(codex_runtime, "_exact_stop_live", lambda *_a: live)
    monkeypatch.setattr(codex_runtime, "_fence_exact_live", fence)
    monkeypatch.setattr(codex_runtime, "_release_delivery_event_barrier", release_barrier)
    monkeypatch.setattr(codex_runtime, "_finish_interrupted_if_still_owned", finish)
    monkeypatch.setattr(codex_runtime, "_release_exact_live", lambda *_a: None)
    try:
        assert await codex_runtime.stop(
            "thread-codex",
            expected_turn_id="turn-codex",
        ) is True
        assert live.checkpoint_wait_scope.stop_requested is True
        assert calls == [
            ("terminate", "turn-codex"),
            ("fence", "turn-codex"),
            ("finish", "turn-codex"),
        ]
    finally:
        _LIVE.pop("thread-codex", None)


@pytest.mark.asyncio
async def test_stale_codex_stop_does_not_signal_successor_scope() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_runtime_state import _LIVE, _LiveCodex

    thread_id = "codex-stale-stop"
    successor = _LiveCodex(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="codex",
        corsia_turn=TurnIdentity("turn-successor", 2),
    )
    _LIVE.put(thread_id, successor)
    try:
        assert await codex_runtime.stop(
            thread_id,
            expected_turn_id="turn-predecessor",
        ) is False
        assert successor.checkpoint_wait_scope.stop_requested is False
    finally:
        _LIVE.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_owns_same_turn_checkpoint_replacement_before_write(
    isolated_db,
    monkeypatch,
) -> None:
    """Stop's exact-Turn latch follows a checkpoint replacement before write."""
    from infra.database import get_db
    from subagents import codex_runtime, codex_turn_transitions

    thread_id = "codex-stop-checkpoint-replacement"
    identity = _pending_switch_turn(
        thread_id,
        source_model="codex/source",
        destination_model="codex/destination",
    )
    target = SimpleNamespace(
        harness="codex",
        available=True,
        catalog_id="codex/source",
        transport_model="gpt-5.6-luna",
        context_window=128_000,
        windows=WindowFacts(128_000, None),
        expected_reported_model="gpt-5.6-luna",
    )
    prepared = SimpleNamespace(runtime_context_epoch=1,
                               fit_ledger=SimpleNamespace(rendered_input_tokens=100))
    old_stop_requested = asyncio.Event()
    replacement_lives = []
    provider_writes = []
    delivery_events = []
    driven = []

    class Session:
        binary = "codex"
        working_dir = None
        project_id = None
        effort = None
        returncode = None
        request_log = ()

        def __init__(self, label: str) -> None:
            self.label = label
            self.codex_thread_id = f"provider-{label}"
            self._closing = False
            self.calls: list[str] = []

        def _request_terminate(self) -> None:
            self._closing = True
            self.calls.append("request_terminate")

        def revoke_dynamic_tool_admission(self) -> None:
            self.calls.append("revoke_admission")

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            self.calls.append("terminate")

    old_session = Session("old")
    old_live = codex_runtime._LiveCodex(
        codex_thread_id=old_session.codex_thread_id,
        session=old_session,
        parent_thread_id=None,
        name="old",
        model=target.transport_model,
        catalog_model_id=target.catalog_id,
        corsia_turn=identity,
    )
    old_scope = old_live.checkpoint_wait_scope
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, old_live)
    codex_runtime._APP_SESSIONS[thread_id] = old_session
    codex_runtime._CODEX_THREADS[thread_id] = old_session.codex_thread_id
    transition_lock = codex_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()

    async def terminate_foreground(_thread_id, live) -> bool:
        if live is old_live:
            old_stop_requested.set()
        return True

    async def configure_replacement(_session, live, _spec, **_kwargs):
        session = Session("replacement")
        live.session = session
        live.codex_thread_id = session.codex_thread_id
        replacement_lives.append(live)

        async def start_input(*_args) -> bool:
            provider_writes.append(True)
            return True

        return session, start_input

    async def accept_replacement(_spec, _live, start_input, _bcast) -> bool:
        return await start_input()

    async def reject_generic_failed_spawn(*_args, **_kwargs) -> None:
        raise AssertionError("shared Stop latch entered generic spawn failure")

    def transfer_delivery_to_turn() -> None:
        raise AssertionError("same-Turn checkpoint replacement transferred delivery again")

    async def settle_delivery_terminal(
        outcome: str,
        _error: str | None,
    ) -> None:
        assert outcome == "cancelled"
        delivery_events.append("cancelled")

    async def settle_delivery() -> None:
        delivery_events.append("accepted")

    async def drive(settlement) -> None:
        assert transition_lock.locked() is False
        driven.append(settlement)

    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model: target,
    )
    monkeypatch.setattr(
        codex_turn_transitions, "_require_provider_fit", lambda *_args: None,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_resolve_spawn_payload",
        lambda **_kwargs: SimpleNamespace(
            provider_text="checkpoint continuation",
            reconstruction=SimpleNamespace(
                snapshot=SimpleNamespace(to_dict=lambda: {}),
            ),
            history_items=(),
            tool_registry=object(),
            dynamic_tools=(),
            continuity_accounting=ContinuityAccounting.not_delivered(),
        ),
    )
    monkeypatch.setattr(
        codex_runtime,
        "_resolve_app_server_tools",
        lambda *_args, **_kwargs: (object(), ()),
    )
    monkeypatch.setattr(
        codex_runtime, "_configure_spec_app_server_input", configure_replacement,
    )
    monkeypatch.setattr(
        codex_runtime, "_accept_spec_app_server_input", accept_replacement,
    )
    monkeypatch.setattr(
        codex_runtime, "_finish_failed_app_server_spawn", reject_generic_failed_spawn,
    )
    monkeypatch.setattr(
        codex_runtime, "_terminate_turn_foreground_process", terminate_foreground,
    )
    monkeypatch.setattr(codex_runtime, "_default_broadcast", lambda *_args: None)
    monkeypatch.setattr(
        codex_runtime, "_retire_registered_thread_artifacts", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(codex_turn_transitions, "drive_turn_settlement", drive)

    stop_task = asyncio.create_task(codex_runtime.stop(
        thread_id,
        expected_turn_id=identity.turn_id,
        expected_live=old_live,
        broadcast=lambda *_args: None,
    ))
    try:
        await asyncio.wait_for(old_stop_requested.wait(), timeout=0.5)
        assert old_scope.stop_requested is True
        assert stop_task.done() is False

        with pytest.raises(asyncio.CancelledError):
            await codex_turn_transitions.resume_same_turn_after_checkpoint_locked(
                thread_id,
                old_live,
                prepared,
                current_user_text="continue",
                settle_delivery=settle_delivery,
                transfer_delivery_to_turn=transfer_delivery_to_turn,
                settle_delivery_terminal=settle_delivery_terminal,
                held_lock=transition_lock,
            )

        replacement = replacement_lives[0]
        assert codex_runtime._LIVE.get(thread_id) is replacement
        assert replacement.corsia_turn == identity
        assert replacement.checkpoint_wait_scope is old_scope
        assert replacement.checkpoint_wait_scope.stop_requested is True
        assert provider_writes == []
        assert delivery_events == ["cancelled"]

        transition_lock.release()
        assert await asyncio.wait_for(stop_task, timeout=0.5) is True
        assert replacement.session.calls == [
            "request_terminate",
            "revoke_admission",
            "terminate",
        ]
        assert codex_runtime._LIVE.get(thread_id) is None
        assert len(driven) == 1
        db = get_db()
        try:
            assert db.execute(
                "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
            ).fetchone()["status"] == "cancelled"
        finally:
            db.close()
    finally:
        if transition_lock.locked():
            transition_lock.release()
        await asyncio.gather(stop_task, return_exceptions=True)
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_latches_replacement_before_session_configuration(
    monkeypatch,
) -> None:
    """A replacement without a transport still receives its exact Stop latch."""
    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    thread_id = "codex-stop-before-replacement-configure"
    identity = TurnIdentity("turn-codex-replacement", 1)
    persist_turn(thread_id, identity.turn_id)
    wait_scope = CheckpointWaitScope()
    configure_entered = asyncio.Event()
    release_configure = asyncio.Event()
    request_attempted = asyncio.Event()
    provider_writes = []

    class Session:
        returncode = None

        def __init__(self) -> None:
            self._closing = False

        def _request_terminate(self) -> None:
            self._closing = True

        def revoke_dynamic_tool_admission(self) -> None:
            return None

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            return None

    async def configure(_session, live, _spec, **_kwargs):
        assert live.session is None
        configure_entered.set()
        await release_configure.wait()
        session = Session()
        live.session = session

        async def start_input(*_args) -> bool:
            provider_writes.append(True)
            return True

        return session, start_input

    async def accept(_spec, _live, start_input, _bcast) -> bool:
        return await start_input()

    async def finish_stop(*_args, **_kwargs):
        return True, None

    async def reject_generic_failed_spawn(*_args, **_kwargs) -> None:
        raise AssertionError("Stop-owned replacement used generic spawn failure")

    original_request_stop = codex_runtime._request_exact_stop

    def request_stop(*args, **kwargs):
        request_attempted.set()
        return original_request_stop(*args, **kwargs)

    monkeypatch.setattr(
        codex_runtime,
        "_resolve_app_server_tools",
        lambda *_args, **_kwargs: (object(), ()),
    )
    monkeypatch.setattr(
        codex_runtime, "_validate_codex_transport_model", lambda model: model,
    )
    monkeypatch.setattr(codex_runtime, "_make_on_event", lambda *_args: None)
    monkeypatch.setattr(
        codex_runtime, "_configure_spec_app_server_input", configure,
    )
    monkeypatch.setattr(codex_runtime, "_accept_spec_app_server_input", accept)
    monkeypatch.setattr(
        codex_runtime, "_finish_failed_app_server_spawn", reject_generic_failed_spawn,
    )
    monkeypatch.setattr(codex_runtime, "_request_exact_stop", request_stop)
    monkeypatch.setattr(
        codex_runtime,
        "_terminate_turn_foreground_process",
        lambda *_args: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        codex_runtime, "_finish_interrupted_if_still_owned", finish_stop,
    )

    codex_runtime._LIVE.reset()
    transition_lock = codex_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    spawn_task = asyncio.create_task(codex_runtime._spawn_app_server_turn(
        thread_id,
        "checkpoint continuation",
        current_user_text="continue",
        successor_turn_id=identity.turn_id,
        codex_thread_id=None,
        binary="codex",
        working_dir=None,
        broadcast=lambda *_args: None,
        model="gpt-5.6-luna",
        catalog_model_id="codex/source",
        windows=WindowFacts(128_000, None),
        expected_reported_model="gpt-5.6-luna",
        preadmitted_turn=identity,
        continuing_wait_scope=wait_scope,
        held_lock=transition_lock,
    ))
    stop_task = None
    try:
        await asyncio.wait_for(configure_entered.wait(), timeout=0.5)
        replacement = codex_runtime._LIVE.get(thread_id)
        assert replacement is not None
        assert replacement.session is None
        assert replacement.checkpoint_wait_scope is wait_scope

        stop_task = asyncio.create_task(codex_runtime.stop(
            thread_id, expected_turn_id=identity.turn_id,
        ))
        await asyncio.wait_for(request_attempted.wait(), timeout=0.5)
        assert wait_scope.stop_requested is True
        assert stop_task.done() is False
        assert provider_writes == []

        release_configure.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(spawn_task, timeout=0.5)
        assert provider_writes == []

        transition_lock.release()
        assert await asyncio.wait_for(stop_task, timeout=0.5) is True
        assert codex_runtime._LIVE.get(thread_id) is None
    finally:
        release_configure.set()
        if transition_lock.locked():
            transition_lock.release()
        tasks = [spawn_task, *([stop_task] if stop_task is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_owns_replacement_session_start_prewrite(
    monkeypatch,
) -> None:
    """Stop settles a replacement killed after its open-scope check."""
    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    thread_id = "codex-stop-during-replacement-start"
    identity = TurnIdentity("turn-codex-replacement-start", 1)
    persist_turn(thread_id, identity.turn_id)
    wait_scope = CheckpointWaitScope()
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    stop_requested = asyncio.Event()
    provider_writes = []
    delivery_events = []

    class Session:
        returncode = None
        codex_thread_id = "provider-replacement"

        def __init__(self) -> None:
            self._closing = False

        def _request_terminate(self) -> None:
            self._closing = True
            stop_requested.set()

        def revoke_dynamic_tool_admission(self) -> None:
            return None

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            return None

    session = Session()

    async def configure(_session, live, _spec, **_kwargs):
        live.session = session

        async def start_input(on_write, _on_drain) -> bool:
            start_entered.set()
            await release_start.wait()
            if session._closing:
                raise RuntimeError("terminated before provider write")
            on_write()
            provider_writes.append(True)
            return True

        return session, start_input

    async def terminate_foreground(*_args) -> bool:
        return True

    async def finish_stop(*_args, **_kwargs):
        return True, None

    async def reject_generic_failed_spawn(*_args, **_kwargs) -> None:
        raise AssertionError("Stop-owned prewrite used generic spawn failure")

    def transfer_delivery_to_turn() -> None:
        raise AssertionError("same-Turn checkpoint replacement transferred delivery again")

    async def settle_delivery_terminal(
        outcome: str,
        _error: str | None,
    ) -> None:
        delivery_events.append(outcome)

    async def settle_delivery() -> None:
        delivery_events.append("accepted")

    monkeypatch.setattr(
        codex_runtime,
        "_resolve_app_server_tools",
        lambda *_args, **_kwargs: (object(), ()),
    )
    monkeypatch.setattr(
        codex_runtime, "_validate_codex_transport_model", lambda model: model,
    )
    monkeypatch.setattr(codex_runtime, "_make_on_event", lambda *_args: None)
    monkeypatch.setattr(
        codex_runtime, "_configure_spec_app_server_input", configure,
    )
    monkeypatch.setattr(
        codex_runtime, "_finish_failed_app_server_spawn", reject_generic_failed_spawn,
    )
    monkeypatch.setattr(
        codex_runtime, "_terminate_turn_foreground_process", terminate_foreground,
    )
    monkeypatch.setattr(
        codex_runtime, "_finish_interrupted_if_still_owned", finish_stop,
    )

    codex_runtime._LIVE.reset()
    transition_lock = codex_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    spawn_task = asyncio.create_task(codex_runtime._spawn_app_server_turn(
        thread_id,
        "checkpoint continuation",
        current_user_text="continue",
        successor_turn_id=identity.turn_id,
        codex_thread_id=None,
        binary="codex",
        working_dir=None,
        broadcast=lambda *_args: None,
        model="gpt-5.6-luna",
        catalog_model_id="codex/source",
        windows=WindowFacts(128_000, None),
        expected_reported_model="gpt-5.6-luna",
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=identity,
        continuing_wait_scope=wait_scope,
        held_lock=transition_lock,
    ))
    stop_task = None
    try:
        await asyncio.wait_for(start_entered.wait(), timeout=0.5)
        replacement = codex_runtime._LIVE.get(thread_id)
        assert replacement is not None
        assert replacement.session is session
        assert wait_scope.stop_requested is False

        stop_task = asyncio.create_task(codex_runtime.stop(
            thread_id, expected_turn_id=identity.turn_id,
        ))
        await asyncio.wait_for(stop_requested.wait(), timeout=0.5)
        assert session._closing is True
        assert replacement.provider_context_untrusted is True
        assert wait_scope.stop_requested is True
        assert stop_task.done() is False

        release_start.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(spawn_task, timeout=0.5)
        assert provider_writes == []
        assert delivery_events == ["cancelled"]

        transition_lock.release()
        assert await asyncio.wait_for(stop_task, timeout=0.5) is True
        assert codex_runtime._LIVE.get(thread_id) is None
    finally:
        release_start.set()
        if transition_lock.locked():
            transition_lock.release()
        tasks = [spawn_task, *([stop_task] if stop_task is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_does_not_reclassify_postwrite_start_failure() -> None:
    """A Stop latch cannot erase observed provider-write ambiguity."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    thread_id = "codex-stop-postwrite-start"
    session = SimpleNamespace(_closing=True)
    live = codex_runtime._LiveCodex(
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=TurnIdentity("turn-codex-postwrite", 1),
    )
    live.checkpoint_wait_scope.request_stop()
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, live)

    async def fail_after_write(on_write, _on_drain):
        on_write()
        raise RuntimeError("provider response lost after write")

    try:
        with pytest.raises(RuntimeError, match="response lost after write"):
            await codex_runtime._accept_app_server_input(
                thread_id,
                live,
                fail_after_write,
                mark_accepted=None,
                bcast=lambda *_args: None,
            )
        assert live.input_delivery_unproven is True
    finally:
        codex_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_codex_mid_turn_stop_keeps_terminal_ownership_after_replacement(
    monkeypatch,
) -> None:
    """Inherited Stop cancellation cannot become checkpoint rejection error."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime, codex_turn_transitions

    thread_id = "codex-mid-turn-stop-replacement"
    identity = TurnIdentity("turn-codex-mid-turn", 1)
    target = SimpleNamespace(
        harness="codex",
        available=True,
        catalog_id="codex/source",
        transport_model="gpt-5.6-luna",
        context_window=128_000,
        windows=WindowFacts(128_000, None),
        expected_reported_model="gpt-5.6-luna",
    )
    session = SimpleNamespace(
        binary="codex",
        working_dir=None,
        project_id=None,
        effort=None,
    )
    old_live = codex_runtime._LiveCodex(
        session=session,
        parent_thread_id=None,
        name="old",
        catalog_model_id=target.catalog_id,
        corsia_turn=identity,
    )
    old_live.checkpoint_wait_scope.request_stop()
    rejected = []

    async def spawn_replacement(*_args, **kwargs):
        replacement = codex_runtime._LiveCodex(
            session=SimpleNamespace(),
            parent_thread_id=None,
            name="replacement",
            corsia_turn=identity,
            checkpoint_wait_scope=kwargs["continuing_wait_scope"],
        )
        codex_runtime._LIVE.put(thread_id, replacement)
        raise asyncio.CancelledError("Stop owns checkpoint replacement")

    async def reject_input(*_args, **_kwargs) -> None:
        rejected.append(True)

    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model: target,
    )
    monkeypatch.setattr(
        codex_runtime, "_fence_exact_live", lambda *_args: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        codex_runtime, "_release_delivery_event_barrier", lambda *_args: asyncio.sleep(0),
    )
    monkeypatch.setattr(codex_runtime, "_spawn_turn", spawn_replacement)
    monkeypatch.setattr(codex_runtime, "_finish_rejected_input", reject_input)
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, old_live)
    try:
        with pytest.raises(asyncio.CancelledError):
            await codex_turn_transitions.resume_same_turn_after_checkpoint_locked(
                thread_id,
                old_live,
                SimpleNamespace(runtime_context_epoch=1),
                current_user_text="mid-turn continuation",
                held_lock=codex_runtime._LIVE.lock(thread_id),
            )
        replacement = codex_runtime._LIVE.get(thread_id)
        assert replacement is not None and replacement is not old_live
        assert replacement.corsia_turn == identity
        assert replacement.checkpoint_wait_scope is old_live.checkpoint_wait_scope
        assert replacement.checkpoint_wait_scope.stop_requested is True
        assert rejected == []
        assert old_live.corsia_turn is None
    finally:
        codex_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_codex_stale_stop_does_not_latch_unbound_successor(
    monkeypatch,
) -> None:
    """A published successor is not Stop-addressable until its Turn binds."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    thread_id = "codex-stale-stop-unbound-successor"
    successor_identity = TurnIdentity("turn-successor", 2)
    persist_turn(thread_id, successor_identity.turn_id)
    successor_published = asyncio.Event()
    release_successor_bind = asyncio.Event()
    successor_ref = []
    provider_writes = []

    class Session:
        returncode = None
        codex_thread_id = "provider-successor"
        request_log = ()

    session = Session()
    original_start_live_turn = codex_runtime._start_live_turn

    async def pause_before_bind(thread_id, live, text, **kwargs):
        assert live.corsia_turn is None
        successor_ref.append(live)
        successor_published.set()
        await release_successor_bind.wait()
        return await original_start_live_turn(thread_id, live, text, **kwargs)

    async def configure(_session, live, _spec, **_kwargs):
        live.session = session

        async def start_input(on_write, _on_drain) -> bool:
            on_write()
            provider_writes.append(True)
            return True

        return session, start_input

    monkeypatch.setattr(codex_runtime, "_start_live_turn", pause_before_bind)
    monkeypatch.setattr(
        codex_runtime,
        "_resolve_app_server_tools",
        lambda *_args, **_kwargs: (object(), ()),
    )
    monkeypatch.setattr(
        codex_runtime, "_validate_codex_transport_model", lambda model: model,
    )
    monkeypatch.setattr(codex_runtime, "_make_on_event", lambda *_args: None)
    monkeypatch.setattr(
        codex_runtime, "_configure_spec_app_server_input", configure,
    )

    codex_runtime._LIVE.reset()
    transition_lock = codex_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    spawn_task = asyncio.create_task(codex_runtime._spawn_app_server_turn(
        thread_id,
        "successor input",
        current_user_text="successor input",
        successor_turn_id=successor_identity.turn_id,
        codex_thread_id=None,
        binary="codex",
        working_dir=None,
        broadcast=lambda *_args: None,
        model="gpt-5.6-luna",
        catalog_model_id="codex/source",
        windows=WindowFacts(128_000, None),
        expected_reported_model="gpt-5.6-luna",
        preadmitted_turn=successor_identity,
        held_lock=transition_lock,
    ))
    try:
        await asyncio.wait_for(successor_published.wait(), timeout=0.5)
        successor = successor_ref[0]
        assert codex_runtime._LIVE.get(thread_id) is successor
        assert successor.corsia_turn is None

        assert await codex_runtime.stop(
            thread_id, expected_turn_id="turn-predecessor",
        ) is False
        assert successor.checkpoint_wait_scope.stop_requested is False

        release_successor_bind.set()
        assert await asyncio.wait_for(spawn_task, timeout=0.5) == successor_identity
        assert successor.corsia_turn == successor_identity
        assert provider_writes == [True]
    finally:
        release_successor_bind.set()
        await asyncio.gather(spawn_task, return_exceptions=True)
        transition_lock.release()
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_does_not_follow_different_turn_replacement(
    monkeypatch,
) -> None:
    """A captured Stop never signals a successor installed behind its lock."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    thread_id = "codex-stop-different-turn-replacement"
    captured = codex_runtime._LiveCodex(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="captured",
        corsia_turn=TurnIdentity("turn-captured", 1),
    )
    successor = codex_runtime._LiveCodex(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="successor",
        corsia_turn=TurnIdentity("turn-successor", 2),
    )
    captured_halted = asyncio.Event()
    requested = []

    def request_exact(_thread_id, _turn_id, live):
        requested.append(live)
        live.checkpoint_wait_scope.request_stop()
        return live, live.session, None

    async def terminate_foreground(*_args) -> bool:
        captured_halted.set()
        return True

    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, captured)
    transition_lock = codex_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()
    monkeypatch.setattr(codex_runtime, "_request_exact_stop", request_exact)
    monkeypatch.setattr(
        codex_runtime, "_terminate_turn_foreground_process", terminate_foreground,
    )
    stop_task = asyncio.create_task(codex_runtime.stop(
        thread_id,
        expected_turn_id="turn-captured",
        expected_live=captured,
    ))
    try:
        await asyncio.wait_for(captured_halted.wait(), timeout=0.5)
        codex_runtime._LIVE.put(thread_id, successor)
        transition_lock.release()

        assert await asyncio.wait_for(stop_task, timeout=0.5) is False
        assert requested == [captured]
        assert captured.checkpoint_wait_scope.stop_requested is True
        assert successor.checkpoint_wait_scope.stop_requested is False
        assert codex_runtime._LIVE.get(thread_id) is successor
    finally:
        if transition_lock.locked():
            transition_lock.release()
        await asyncio.gather(stop_task, return_exceptions=True)
        codex_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_codex_stop_accepts_owner_finishing_during_fence(monkeypatch) -> None:
    """Natural completion during the Stop fence is already settled."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_turn_transitions import _settle_stop_owner

    thread_id = "codex-stop-fence-race"
    session = SimpleNamespace()
    live = codex_runtime._LiveCodex(
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=TurnIdentity("turn-codex", 1),
    )
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, live)

    async def finish_during_fence(*_args) -> bool:
        codex_runtime._LIVE.pop(thread_id, None)
        return True

    async def release_barrier(*_args) -> None:
        return None

    monkeypatch.setattr(codex_runtime, "_fence_exact_live", finish_during_fence)
    monkeypatch.setattr(
        codex_runtime, "_release_delivery_event_barrier", release_barrier,
    )

    stopped, settlement = await _settle_stop_owner(
        thread_id,
        "turn-codex",
        (live, session, None),
        None,
    )

    assert stopped is True
    assert settlement is None


@pytest.mark.asyncio
async def test_codex_stop_drives_pending_switch_after_releasing_transition_lock(
    isolated_db,
    monkeypatch,
) -> None:
    """A stopped Codex source cannot recursively acquire its transition lock."""
    from infra import thread_execution
    from subagents import codex_runtime

    thread_id = "codex-stop-pending-switch"
    source_model = "codex/source"
    destination_model = "codex/destination"
    identity = _pending_switch_turn(
        thread_id,
        source_model=source_model,
        destination_model=destination_model,
    )
    events: list[str] = []

    class Session:
        def _request_terminate(self) -> None:
            events.append("runtime_halted")

        def revoke_dynamic_tool_admission(self) -> None:
            return None

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            events.append("runtime_retired")

    session = Session()
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-provider-thread",
        session=session,
        parent_thread_id=None,
        name="codex",
        model="source",
        catalog_model_id=source_model,
        corsia_turn=identity,
    )
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, live)
    codex_runtime._APP_SESSIONS[thread_id] = session
    transition_lock = codex_runtime._LIVE.lock(thread_id)

    async def terminate_foreground(*_args) -> bool:
        return True

    async def drive(settlement) -> None:
        assert settlement.switch_request is not None
        assert settlement.switch_request.destination_model == destination_model
        assert codex_runtime._LIVE.get(thread_id) is None
        assert transition_lock.locked() is False
        events.append("settlement_driven")

    monkeypatch.setattr(
        codex_runtime,
        "_terminate_turn_foreground_process",
        terminate_foreground,
    )
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    try:
        assert await asyncio.wait_for(
            codex_runtime.stop(thread_id, expected_turn_id=identity.turn_id),
            timeout=0.5,
        ) is True
        assert events == [
            "runtime_halted",
            "runtime_retired",
            "settlement_driven",
        ]
    finally:
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_drives_committed_switch_before_raising_marker_error(
    isolated_db,
    monkeypatch,
) -> None:
    """A post-commit Codex marker error cannot strand the committed successor."""
    from infra import thread_execution
    from subagents import codex_runtime

    thread_id = "codex-stop-marker-error"
    destination_model = "codex/destination"
    identity = _pending_switch_turn(
        thread_id,
        source_model="codex/source",
        destination_model=destination_model,
    )
    events: list[str] = []
    publication_error = RuntimeError("Codex terminal marker failed")

    class Session:
        def _request_terminate(self) -> None:
            events.append("runtime_halted")

        def revoke_dynamic_tool_admission(self) -> None:
            return None

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            events.append("runtime_retired")

    session = Session()
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-provider-thread", session=session,
        parent_thread_id=None, name="codex", model="source",
        catalog_model_id="codex/source", corsia_turn=identity,
    )
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, live)
    codex_runtime._APP_SESSIONS[thread_id] = session
    transition_lock = codex_runtime._LIVE.lock(thread_id)

    async def terminate_foreground(*_args) -> bool:
        return True

    async def fail_marker(*_args, **_kwargs):
        events.append("publication_failed")
        raise publication_error

    async def drive(settlement) -> None:
        assert settlement.switch_request is not None
        assert settlement.switch_request.destination_model == destination_model
        assert codex_runtime._LIVE.get(thread_id) is None
        assert transition_lock.locked() is False
        events.append("settlement_driven")

    monkeypatch.setattr(
        codex_runtime, "_terminate_turn_foreground_process", terminate_foreground,
    )
    monkeypatch.setattr(codex_runtime, "record_terminal_marker", fail_marker)
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    try:
        with pytest.raises(RuntimeError) as raised:
            await asyncio.wait_for(
                codex_runtime.stop(thread_id, expected_turn_id=identity.turn_id),
                timeout=0.5,
            )
        assert raised.value is publication_error
        assert events == [
            "runtime_halted", "runtime_retired", "publication_failed",
            "settlement_driven",
        ]
    finally:
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_codex_stop_finishes_after_caller_cancelled_post_halt(
    isolated_db,
    monkeypatch,
) -> None:
    """Caller cancellation cannot strand a Turn after Codex has been halted."""
    from infra import thread_execution
    from subagents import codex_runtime

    thread_id = "codex-stop-caller-cancelled"
    identity = _pending_switch_turn(
        thread_id,
        source_model="codex/source",
        destination_model="codex/destination",
    )
    halted = asyncio.Event()
    release_halt = asyncio.Event()
    events: list[str] = []

    class Session:
        def _request_terminate(self) -> None:
            events.append("runtime_halted")

        def revoke_dynamic_tool_admission(self) -> None:
            return None

        async def wait_dynamic_tool_requests_idle(self) -> None:
            return None

        async def terminate(self) -> None:
            events.append("runtime_retired")

    session = Session()
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-provider-thread", session=session,
        parent_thread_id=None, name="codex", model="source",
        catalog_model_id="codex/source", corsia_turn=identity,
    )
    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, live)
    codex_runtime._APP_SESSIONS[thread_id] = session

    async def terminate_foreground(*_args) -> bool:
        halted.set()
        await release_halt.wait()
        return True

    async def drive(_settlement) -> None:
        assert codex_runtime._LIVE.get(thread_id) is None
        events.append("settlement_driven")

    monkeypatch.setattr(
        codex_runtime, "_terminate_turn_foreground_process", terminate_foreground,
    )
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive)
    task = asyncio.create_task(
        codex_runtime.stop(thread_id, expected_turn_id=identity.turn_id)
    )
    try:
        await asyncio.wait_for(halted.wait(), timeout=0.5)
        task.cancel()
        release_halt.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert events == [
            "runtime_halted", "runtime_retired", "settlement_driven",
        ]
    finally:
        release_halt.set()
        codex_runtime._LIVE.reset()
        codex_runtime._APP_SESSIONS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_stop_receipt_comes_from_canonical_terminal(
    isolated_db, monkeypatch,
) -> None:
    """A harness Stop wins the one Turn CAS and returns its exact receipt."""
    from core.turn_core import settle_turn_terminal
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from infra import state
    from subagents import cc_runtime, codex_runtime
    from ws.stop_control import do_stop

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/sonnet','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1", role="user", content="work", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    db.close()
    assert admission.batch is not None

    async def _cc_stop(_thread_id, *, expected_turn_id, **_kwargs):
        assert expected_turn_id == "turn-1"
        await settle_turn_terminal(expected_turn_id, "cancelled")
        return True

    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: True)
    monkeypatch.setattr(cc_runtime, "stop", _cc_stop)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(state, "owner_broadcast", lambda _uid: lambda *_a: None)

    async def _owns(_user, _sid):
        return True

    receipt = await do_stop(
        {"id": "user-1"},
        "thread-1",
        None,
        None,
        "turn-1",
        owns_thread_fn=_owns,
    )

    assert receipt.outcome == "stopped"
    assert receipt.turn_id == "turn-1"
    db = get_db()
    try:
        assert db.execute(
            "SELECT status FROM active_turns WHERE id='turn-1'"
        ).fetchone()[0] == "cancelled"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cc_stop_fails_loudly_when_transition_lock_stays_held(monkeypatch) -> None:
    """Provider work is halted, then Stop refuses within the bound and never closes
    the owner without the lock or releases a lock it did not take (2026-09-16)."""
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime, cc_stop, live_registry

    thread_id = "cc-stop-lock-held-by-stalled-transition"
    identity = TurnIdentity("turn-cc-stalled-holder", 1)
    live = cc_runtime._Live(
        session=SimpleNamespace(), parent_thread_id=None, name="stalled",
        corsia_turn=identity, turn_active=True,
    )
    halted = []

    async def halt_exact(*_args) -> bool:
        halted.append(cc_runtime._LIVE.get(thread_id))
        return True

    async def reject_close(*_args, **_kwargs):
        raise AssertionError("Stop closed the owner without holding the transition lock")

    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put(thread_id, live)
    transition_lock = cc_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()  # the stalled holder never releases
    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cc_stop, "halt_exact_live_before_transition", halt_exact)
    monkeypatch.setattr(cc_stop, "_close_live_for_stop", reject_close)
    try:
        assert await asyncio.wait_for(
            cc_stop.stop(thread_id, expected_turn_id=identity.turn_id), timeout=2.0,
        ) is False
        assert halted == [live]
        assert cc_runtime._LIVE.get(thread_id) is live
        assert transition_lock.locked() is True
    finally:
        transition_lock.release()
        cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_codex_stop_fails_loudly_when_transition_lock_stays_held(monkeypatch) -> None:
    """Codex Stop used to wait on the transition lock with no bound; a stalled
    holder hung the user's Stop forever. It now fails within the shared bound."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime, codex_turn_transitions, live_registry

    thread_id = "codex-stop-lock-held-by-stalled-transition"
    identity = TurnIdentity("turn-codex-stalled-holder", 1)

    class Session:
        codex_thread_id = "codex-provider-thread"

        def _request_terminate(self) -> None:
            return None

        async def terminate(self) -> None:
            return None

    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-provider-thread", session=Session(), parent_thread_id=None,
        name="stalled", model="codex/source", catalog_model_id="codex/source",
        corsia_turn=identity,
    )

    async def request_owner(_thread_id, _expected_turn_id, _expected_live):
        return live, live.session, None

    async def reject_settle(*_args, **_kwargs):
        raise AssertionError("Stop settled the owner without holding the transition lock")

    codex_runtime._LIVE.reset()
    codex_runtime._LIVE.put(thread_id, live)
    transition_lock = codex_runtime._LIVE.lock(thread_id)
    await transition_lock.acquire()  # the stalled holder never releases
    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(codex_turn_transitions, "_request_stop_owner", request_owner)
    monkeypatch.setattr(codex_turn_transitions, "_settle_stop_owner", reject_settle)
    try:
        assert await asyncio.wait_for(
            codex_turn_transitions.stop(thread_id, expected_turn_id=identity.turn_id),
            timeout=2.0,
        ) is False
        assert codex_runtime._LIVE.get(thread_id) is live
        assert transition_lock.locked() is True
    finally:
        transition_lock.release()
        codex_runtime._LIVE.reset()
