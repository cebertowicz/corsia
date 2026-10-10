"""#2261 — the native start handshake must not strand its waiter.

``start_ack`` settles from turn-task completion: a turn cancelled before its
first step runs no handler at all, so nothing inside the turn can reject the
handoff. Completion-side settlement (``settle_start_ack_on_completion``) is
the single owner of that rule; the in-turn reject sites (admission failure,
preemption, model-unavailable) remain early backstops that settle the same
future sooner. No waiter can park on a dead handoff, whichever side is
cancelled — the Runner.close ``_cancel_all_tasks`` hang that stalled the
core-cr shard.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from core.turn_core import TurnIdentity
from infra.turn_start_ack import await_native_start
from subagents.cc_stream import CCStreamSession
from tests.conftest import _join_terminal_driver


def _bare_session() -> CCStreamSession:
    """A real-class session shell for exercising the reap's contract."""
    return object.__new__(CCStreamSession)


def _identity() -> TurnIdentity:
    return TurnIdentity("11111111-2222-3333-4444-555555555555", 1)


async def _never_resolves(start_ack: asyncio.Future) -> None:
    await asyncio.Event().wait()
    start_ack.set_result(_identity())


@pytest.mark.asyncio
async def test_waiter_unblocks_when_turn_cancelled_by_non_waiter() -> None:
    """A turn cancelled while parked before its handoff resolves nothing itself.

    Whoever cancels the turn (a preempt or stop, not the waiter), the waiter
    must unblock promptly instead of parking on the abandoned future forever.
    """
    loop = asyncio.get_running_loop()
    start_ack = loop.create_future()
    producer = asyncio.create_task(_never_resolves(start_ack))
    waiter = asyncio.create_task(await_native_start(producer, start_ack))

    # Park the waiter on the shield, then cancel only the producer.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    producer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=2.0)


@pytest.mark.asyncio
async def test_waiter_gets_turn_failure_when_producer_died_unresolved() -> None:
    """A producer that dies without resolving start_ack strands no one.

    The turn's own failure reaches the waiter even though the waiter itself
    was never cancelled.
    """
    loop = asyncio.get_running_loop()
    start_ack = loop.create_future()

    async def producer() -> None:
        raise RuntimeError("admission failed before start handshake")

    turn_task = asyncio.create_task(producer())
    waiter = asyncio.create_task(await_native_start(turn_task, start_ack))

    with pytest.raises(RuntimeError, match="admission failed"):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=2.0)


@pytest.mark.asyncio
async def test_cancelled_waiter_exits_when_producer_is_parked_before_handoff() -> None:
    """A producer cancelled while parked awaiting its handoff never resolves start_ack.

    The waiter must complete promptly instead of parking on the abandoned
    future forever.
    """
    loop = asyncio.get_running_loop()
    start_ack = loop.create_future()
    producer = asyncio.create_task(_never_resolves(start_ack))
    waiter = asyncio.create_task(await_native_start(producer, start_ack))

    # Park the waiter on the shield, then deliver Runner.close-style
    # cancellation to every task at once.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    producer.cancel()
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=2.0)


@pytest.mark.asyncio
async def test_cancelled_waiter_logs_turn_failure_before_handshake(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancelled waiter still surfaces a turn that died before its handshake.

    Cancellation stays the waiter's caller-visible outcome, but the turn's
    failure is logged rather than silently discarded.
    """
    loop = asyncio.get_running_loop()
    start_ack = loop.create_future()
    release = asyncio.Event()

    async def producer() -> None:
        await release.wait()
        raise RuntimeError("died before start handshake")

    turn_task = asyncio.create_task(producer())
    waiter = asyncio.create_task(await_native_start(turn_task, start_ack))
    caplog.set_level(logging.ERROR, logger="infra.turn_start_ack")

    # Park the waiter on the shield, cancel it into the bounded re-await,
    # then let the turn die so the handshake settles with its failure.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=2.0)

    assert any(
        "native turn failed before its start handshake" in record.message
        for record in caplog.records
        if record.levelno == logging.ERROR
    )


@pytest.mark.asyncio
async def test_waiter_still_joins_a_live_producer_until_handoff() -> None:
    """The ownership handoff itself still completes across cancellation."""
    loop = asyncio.get_running_loop()
    start_ack = loop.create_future()

    async def producer() -> None:
        await asyncio.sleep(0.05)
        start_ack.set_result(_identity())

    turn_task = asyncio.create_task(producer())
    waiter = asyncio.create_task(await_native_start(turn_task, start_ack))
    await asyncio.sleep(0)
    waiter.cancel()

    # The producer is alive and will resolve the handoff: the waiter still
    # joins it (raising the original cancellation afterwards).
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=2.0)
    assert start_ack.done()


@pytest.mark.asyncio
async def test_success_path_returns_identity() -> None:
    loop = asyncio.get_running_loop()
    start_ack = loop.create_future()
    start_ack.set_result(_identity())
    turn_task = asyncio.create_task(_never_resolves(start_ack))
    try:
        identity = await await_native_start(turn_task, start_ack)
    finally:
        turn_task.cancel()
    assert isinstance(identity, TurnIdentity)


@pytest.mark.asyncio
async def test_surviving_terminal_driver_fails_the_teardown_loudly() -> None:
    """A driver that outlives cancel + the join bound is named, not silent."""
    loop = asyncio.get_running_loop()

    async def unkillable() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)

    driver = loop.create_task(unkillable())
    await asyncio.sleep(0)  # let the driver park before it is cancelled
    session = _bare_session()
    session._terminal_driver = driver
    session._terminal_event = None
    with pytest.raises(pytest.fail.Exception, match="_terminal_driver survived cancel"):
        await _join_terminal_driver(session, timeout=0.1)
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await driver


@pytest.mark.asyncio
async def test_reap_joins_successor_installed_after_first_read() -> None:
    """A successor installed after the reap's first read is still reaped.

    ``terminate()`` settles the terminal event inside its own awaits, so its
    ``call_soon``-scheduled done callback can still be queued when the reap
    first reads ``_terminal_driver``/``_terminal_event`` — both already done,
    so the one-shot reap returned without yielding and the callback installed
    the successor afterwards. Left unowned, that successor's cancellation
    defers through the durable parent drive and parks
    ``Runner._cancel_all_tasks`` (#2261, the core-cr teardown hang). The reap
    must drain the callback queue and join the successor it finds there.
    """
    loop = asyncio.get_running_loop()

    async def settle_event() -> None:
        return None

    event = loop.create_task(settle_event())

    def install_successor(done: asyncio.Task) -> None:
        session._terminal_driver = loop.create_task(asyncio.Event().wait())

    event.add_done_callback(install_successor)
    session = _bare_session()
    session._terminal_driver = None
    session._terminal_event = event
    await asyncio.sleep(0)  # event completes; its callback stays queued

    await _join_terminal_driver(session)

    successor = session._terminal_driver
    assert successor is not None and successor.done()


@pytest.mark.asyncio
async def test_renamed_terminal_driver_attribute_fails_instead_of_no_op() -> None:
    """A rename on the real session class must not silently no-op the reap."""
    session = _bare_session()
    session._terminal_event = None
    with pytest.raises(AssertionError, match="no longer exposes _terminal_driver"):
        await _join_terminal_driver(session)


@pytest.mark.asyncio
async def test_sessionless_live_is_skipped_not_failed() -> None:
    """A live registered without a session has no driver to reap."""
    await _join_terminal_driver(None)


@pytest.mark.asyncio
async def test_session_double_is_skipped_not_failed() -> None:
    """Test doubles (SimpleNamespace, fake classes) carry no reap contract."""

    class _FakeSession:
        pass

    await _join_terminal_driver(SimpleNamespace())
    await _join_terminal_driver(_FakeSession())
