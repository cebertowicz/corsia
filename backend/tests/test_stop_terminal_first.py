"""Stop ends the exact Turn durably even when its runtime owner never answers."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from test_ownerless_stop import _no_processes, _stop
from test_stop_receipts import _clear_live_turn_state  # noqa: F401

_BOUND_S = 0.2


class _HungSession:
    """A provider process whose termination hangs until the test ends."""

    returncode = None

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.terminations = 0
        self.released = asyncio.Event()

    def _request_terminate(self) -> None:
        return None

    async def terminate(self) -> None:
        self.terminations += 1
        if self.fails:
            raise RuntimeError("provider process refused to exit")
        await self.released.wait()


class _ExpiredTimeout:
    """A controllable Stop deadline that expires before lock acquisition."""

    def __init__(self, delay: float, observed: list[float]) -> None:
        self.delay = delay
        self.observed = observed

    async def __aenter__(self) -> None:
        self.observed.append(self.delay)
        raise TimeoutError

    async def __aexit__(self, *_args) -> bool:
        return False


def _unresolved_turn(main_db, model: str, turn_id: str = "hung"):
    """Admit a Turn whose provider write was fenced but never settled."""
    from core.turn_core import persist_turn_start
    from infra.input_writes import begin_batch_write
    from infra.thread_driver import admit_input

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    admission = admit_input(
        "main-1", role="user", content="hung input", intent="apply_now",
        message_id=f"{turn_id}:input", turn_id=turn_id, boot_epoch="test-boot",
        admitted_at="t", db=main_db,
    )
    begin_batch_write(
        admission.batch, attempt_id=f"{turn_id}:write", started_at="t", db=main_db,
    )
    identity = persist_turn_start(turn_id, "main-1", model, "hung input", db=main_db)
    main_db.commit()
    return identity


def _hung_live(harness: str, identity, *, fails: bool = False):
    if harness == "cc":
        from subagents.cc_runtime_state import _LIVE, _Live

        live = _Live(
            session=_HungSession(fails), parent_thread_id=None, name="hung",
            corsia_turn=identity, turn_active=True,
        )
        return _LIVE, live, _Live(session=None, parent_thread_id=None, name="next")
    from subagents.codex_runtime_state import _LIVE, _LiveCodex

    live = _LiveCodex(
        session=_HungSession(fails), parent_thread_id=None, name="hung",
        corsia_turn=identity,
    )
    return _LIVE, live, _LiveCodex(session=None, parent_thread_id=None, name="next")


async def _join_new_background(before) -> None:
    from ws.ingress import background_tasks

    pending = background_tasks - before
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("stop_fails", [False, True], ids=["stop-hangs", "stop-fails"])
@pytest.mark.parametrize(("harness", "model"), [
    ("cc", "claude-code/opus"), ("codex", "codex/o3"),
])
@pytest.mark.asyncio
async def test_hung_stop_holding_the_lock_cannot_block_the_next_input(
    main_db, monkeypatch, harness, model, stop_fails,
):
    """One Stop deadline ends the Turn, abandons the stalled owner, and feeds on.

    The runtime Stop either hangs or fails at once; neither proves the
    stalled lock holder is gone.
    """
    from host_access import processes
    from infra import state, thread_execution
    from infra.input_admission import accept_thread_input
    from subagents import live_registry
    from ws.ingress import background_tasks

    expired: list[float] = []
    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", _BOUND_S)
    monkeypatch.setattr(
        live_registry.asyncio, "timeout",
        lambda delay: _ExpiredTimeout(delay, expired),
    )
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", _no_processes)
    main_db.commit()
    identity = _unresolved_turn(main_db, model)
    registry, hung, successor = _hung_live(harness, identity, fails=stop_fails)
    registry.reset()
    registry.put("main-1", hung)
    stalled = registry.lock("main-1")
    await stalled.acquire()
    before = set(background_tasks)
    try:
        assert not await live_registry.acquire_stop_lock(
            stalled, thread_id="main-1", runtime="test",
        )
        assert expired == [_BOUND_S]
        accept_thread_input(
            "main-1", role="user", content="next", intent="queue_after_turn",
            message_id="next", db=main_db,
        )
        frames, delivered = [], []
        delivered_ready = asyncio.Event()

        async def feed_next(batch):
            lock = registry.lock("main-1")
            assert lock is not stalled
            await lock.acquire()
            try:
                registry.publish("main-1", successor, held=lock)
            finally:
                lock.release()
            delivered.append([item.message_id for item in batch.items])
            delivered_ready.set()

        monkeypatch.setattr(thread_execution, "drive_claimed_batch", feed_next)
        monkeypatch.setattr(
            state, "broadcast_event", lambda _sid, _uid, frame: frames.append(frame),
        )
        receipt = await _stop(main_db, identity.turn_id)
        assert receipt.outcome == "stopped"
        assert [frame["type"] for frame in frames] == ["turn_cancelled"]
        assert tuple(main_db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='hung:input'",
        ).fetchone()) == ("ambiguous", "ambiguous")
        await delivered_ready.wait()
        assert delivered == [["next"]]
        assert main_db.execute(
            "SELECT status FROM active_turns WHERE id='hung'",
        ).fetchone()[0] == "cancelled"
        assert hung.checkpoint_wait_scope.stop_requested
        assert hung.session.terminations >= 1
        with pytest.raises(live_registry.StaleTransition):
            registry.publish("main-1", hung, held=stalled)
        assert registry.get("main-1") is successor
    finally:
        hung.session.released.set()
        stalled.release()
        await _join_new_background(before)
        registry.reset()


@pytest.mark.parametrize(("harness", "model"), [
    ("cc", "claude-code/opus"), ("codex", "codex/o3"),
])
@pytest.mark.asyncio
async def test_delete_queued_behind_the_stalled_owner_fails_when_stop_abandons_it(
    main_db, monkeypatch, harness, model,
):
    """A transition already waiting on the stalled lock cannot wait forever.

    Abandon drops that lock generation; its waiters wake and fail fenced
    instead of entering beside the successor or parking on a lock nobody
    will release.
    """
    from host_access import processes
    from subagents import live_registry
    from thread.model_state import ThreadQuiescenceError
    from thread.runtime_boundary import quiesce_thread_runtimes
    from ws.ingress import background_tasks

    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", _BOUND_S)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", _no_processes)
    main_db.commit()
    identity = _unresolved_turn(main_db, model)
    registry, hung, _successor = _hung_live(harness, identity)
    registry.put("main-1", hung)
    stalled = registry.lock("main-1")
    await stalled.acquire()
    delete = asyncio.create_task(quiesce_thread_runtimes("main-1", user_id=None))
    before = set(background_tasks)
    try:
        await asyncio.sleep(0)
        assert not delete.done()
        assert (await _stop(main_db, identity.turn_id)).outcome == "stopped"
        with pytest.raises(ThreadQuiescenceError) as failure:
            await delete
        assert isinstance(failure.value.__cause__, live_registry.StaleTransition)
        assert registry.get("main-1") is None
        assert registry.existing_lock("main-1") is None
    finally:
        delete.cancel()
        hung.session.released.set()
        stalled.release()
        await _join_new_background(before)


@pytest.mark.parametrize(("harness", "model"), [
    ("cc", "claude-code/opus"), ("codex", "codex/o3"),
])
@pytest.mark.asyncio
async def test_preadmitted_bind_after_stop_commit_leaves_no_owner(
    main_db, harness, model,
):
    """A runtime that binds the Turn after Stop ended it unbinds and refuses."""
    from core.turn_core import TurnNotPreadmittedError, bind_preadmitted_turn

    main_db.commit()
    identity = _unresolved_turn(main_db, model)
    _registry, live, _next = _hung_live(harness, identity)
    live.corsia_turn = None
    assert (await _stop(main_db, identity.turn_id)).outcome == "stopped"

    def bind(bound):
        live.corsia_turn = bound

    def unbind(bound):
        if live.corsia_turn == bound:
            live.corsia_turn = None

    with pytest.raises(TurnNotPreadmittedError):
        await bind_preadmitted_turn("main-1", identity, bind, unbind)
    assert live.corsia_turn is None


@pytest.mark.asyncio
async def test_failing_terminal_callback_still_ends_the_native_turn(
    main_db, monkeypatch,
):
    """#1688: a failed delivery callback cannot leave the Turn running unowned."""
    from types import SimpleNamespace

    from core import native_turn_runtime as runtime
    from core.native_turn_control import TurnCancelEvent

    main_db.commit()
    identity = _unresolved_turn(main_db, "test-model")

    async def provider_fails(**_kwargs):
        raise RuntimeError("provider failed")

    async def callback(outcome, _error):
        assert outcome == "error"
        raise RuntimeError("delivery settlement failed")

    monkeypatch.setattr(runtime, "run_turn_with_steps", provider_fails)
    event = TurnCancelEvent()
    event.bind_turn_identity(identity)
    turn = runtime.NativeTurn(
        agent=SimpleNamespace(), thread_id="main-1", content="hung input",
        model="test-model", turn_provider={}, handler=SimpleNamespace(),
        cancel_event=event, identity=identity, on_provider_terminal=callback,
    )
    with pytest.raises(RuntimeError, match="delivery settlement failed"):
        await runtime.run_tracked_turn(turn)
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id='hung'",
    ).fetchone()[0] == "error"
    assert (await _stop(main_db, identity.turn_id)).outcome == "already_terminal"


def test_production_code_never_publishes_through_unfenced_put():
    """Every live handle is published under its current lock generation."""
    backend = Path(__file__).resolve().parents[1]
    offenders = []
    for path in backend.rglob("*.py"):
        relative = path.relative_to(backend)
        if relative.parts[0] in {"tests", ".venv"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "put"
                and "LIVE" in ast.unparse(node.func.value)
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == []
