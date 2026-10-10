"""A WebSocket send issued off the application loop still reaches its socket.

The performance program commits on DB worker threads (``infra.db_async``), and an
after-commit nudge there — ``registry.notifications.notify_changed``,
``infra.thread_updates.notify_changed`` — lands in ``infra.state.ws`` with no
running loop in the calling thread. ``send_to_client`` builds the send coroutine
and then calls ``asyncio.create_task``, which raises ``RuntimeError`` there. The
bare ``except`` in ``_schedule_send`` swallowed it, so the frame was lost AND the
coroutine it had already built was never awaited.

These are red-first: the delivery tests fail on the pre-fix tree because nothing
ever reaches the socket, and the leak test fails on the un-awaited coroutine.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import warnings
from collections.abc import Iterator

import pytest

import infra.app_loop as app_loop
from infra.state import ws as ws_state

# Generous: it bounds "this frame was dropped", never a slow machine. Delivery is
# awaited on the socket's own event, so a fast pass costs microseconds.
_DELIVERY_TIMEOUT = 5.0


class _FakeSocket:
    """Enough of a starlette WebSocket for ``_send_to_client``."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.delivered = asyncio.Event()

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        self.delivered.set()


@pytest.fixture
def ws_registry() -> Iterator:
    """Isolate both client registries, then restore whatever another suite held."""
    previous_users = dict(ws_state._ws_user_clients)
    previous_threads = dict(ws_state._ws_thread_clients)
    ws_state._ws_user_clients.clear()
    ws_state._ws_thread_clients.clear()
    try:
        yield ws_state
    finally:
        ws_state._ws_user_clients.clear()
        ws_state._ws_user_clients.update(previous_users)
        ws_state._ws_thread_clients.clear()
        ws_state._ws_thread_clients.update(previous_threads)


async def _delivered(socket: _FakeSocket, why: str) -> None:
    try:
        await asyncio.wait_for(socket.delivered.wait(), timeout=_DELIVERY_TIMEOUT)
    except asyncio.TimeoutError:  # pragma: no cover - the failure path under test
        pytest.fail(why)


@pytest.mark.parametrize(
    "broadcast",
    ["broadcast_to_user", "broadcast_to_thread", "broadcast_event", "broadcast_all"],
)
@pytest.mark.asyncio
async def test_a_worker_thread_broadcast_reaches_the_registered_socket(
    ws_registry, broadcast
) -> None:
    """The frame a DB worker's after-commit nudge built is not silently dropped."""
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    ws_registry._ws_thread_clients["thread-1"] = [socket]
    # conftest's ``_hide_session_app_loop`` nulls the loop for unit tests, so a
    # test that asserts a delivery owns the capture itself.
    app_loop.capture(asyncio.get_running_loop())
    frame = {"type": "thread_changed", "broadcast": broadcast}
    try:
        if broadcast == "broadcast_to_user":
            await asyncio.to_thread(ws_registry.broadcast_to_user, "user-1", frame)
        elif broadcast == "broadcast_to_thread":
            await asyncio.to_thread(ws_registry.broadcast_to_thread, "thread-1", frame)
        elif broadcast == "broadcast_event":
            await asyncio.to_thread(
                ws_registry.broadcast_event, "thread-1", "user-1", frame
            )
        else:
            await asyncio.to_thread(ws_registry.broadcast_all, frame)
        await _delivered(
            socket, f"{broadcast} issued from a worker thread dropped the frame"
        )
    finally:
        app_loop.clear()

    assert socket.sent == [frame]


@pytest.mark.parametrize(
    "broadcast",
    ["broadcast_to_user", "broadcast_to_thread", "broadcast_event", "broadcast_all"],
)
@pytest.mark.asyncio
async def test_offloop_broadcast_hops_before_reading_registries(
    ws_registry, monkeypatch, broadcast
) -> None:
    """A loop-side registry mutation cannot interrupt a worker's fan-out."""
    socket = _FakeSocket()
    added = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    ws_registry._ws_thread_clients["thread-1"] = [socket]
    submitted: list = []

    def mutate_registry_during_submit(coroutine) -> None:
        submitted.append(coroutine)
        # The old implementation starts fan-out before its first per-socket
        # submit. Mutating that in-progress registry makes it submit again (or,
        # for ``broadcast_all``, invalidates ``bucket.values()``). The corrected
        # implementation has not read any registry yet, so one coroutine owns
        # the whole fan-out.
        if broadcast == "broadcast_to_user":
            ws_registry._ws_user_clients["user-1"].append(added)
        elif broadcast == "broadcast_to_thread":
            ws_registry._ws_thread_clients["thread-1"].append(added)
        elif broadcast == "broadcast_event":
            ws_registry._ws_user_clients["user-1"].append(added)
        else:
            ws_registry._ws_user_clients["user-2"] = []
        coroutine.close()

    monkeypatch.setattr(app_loop, "submit", mutate_registry_during_submit)

    if broadcast == "broadcast_to_user":
        await asyncio.to_thread(
            ws_registry.broadcast_to_user, "user-1", {"type": "thread_changed"}
        )
    elif broadcast == "broadcast_to_thread":
        await asyncio.to_thread(
            ws_registry.broadcast_to_thread, "thread-1", {"type": "thread_changed"}
        )
    elif broadcast == "broadcast_event":
        await asyncio.to_thread(
            ws_registry.broadcast_event, "thread-1", "user-1", {"type": "turn_complete"}
        )
    else:
        await asyncio.to_thread(ws_registry.broadcast_all, {"type": "models_updated"})

    assert len(submitted) == 1


def test_submit_closes_a_coroutine_when_scheduling_races_loop_shutdown(monkeypatch) -> None:
    """A post-check ``call_soon_threadsafe`` failure cannot leak its coroutine."""

    class _ClosingLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, _callback, _coroutine) -> None:
            raise RuntimeError("loop is closing")

    async def work() -> None:
        pass

    coroutine = work()
    monkeypatch.setattr(app_loop, "_LOOP", _ClosingLoop())
    try:
        with pytest.raises(app_loop.AppLoopUnavailable, match="closed while scheduling"):
            app_loop.submit(coroutine)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    finally:
        coroutine.close()


@pytest.mark.asyncio
async def test_offloop_sends_from_one_thread_arrive_in_issue_order(
    ws_registry,
) -> None:
    """One worker's sends are ordered: ``call_soon_threadsafe`` is FIFO."""
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    app_loop.capture(asyncio.get_running_loop())
    frames = [{"type": "thread_changed", "seq": n} for n in range(8)]
    try:
        await asyncio.to_thread(
            lambda: [ws_registry.broadcast_to_user("user-1", f) for f in frames]
        )
        while len(socket.sent) < len(frames):
            await _delivered(
                socket, "only part of the worker thread's sends reached the loop"
            )
    finally:
        app_loop.clear()

    assert socket.sent == frames


@pytest.mark.asyncio
async def test_a_send_with_no_app_loop_logs_the_drop_and_leaks_nothing(
    ws_registry,
) -> None:
    """``AppLoopUnavailable`` is neither raised into the caller nor swallowed."""
    from loguru import logger

    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING", format="{message}")
    app_loop.clear()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await asyncio.to_thread(
                ws_registry.broadcast_to_user, "user-1", {"type": "thread_changed"}
            )
            gc.collect()
    finally:
        logger.remove(sink)

    assert socket.sent == []
    assert [r for r in records if "no application event loop" in r], records
    leaked = [str(w.message) for w in caught if "never awaited" in str(w.message)]
    assert not leaked, leaked


@pytest.mark.asyncio
async def test_the_on_loop_path_still_creates_its_task_and_never_hops(
    ws_registry, monkeypatch
) -> None:
    """An on-loop broadcast is untouched: same task creation, no app-loop hop."""
    from unittest.mock import Mock

    submitted = Mock()
    monkeypatch.setattr(app_loop, "submit", submitted)
    real_send_to_client = ws_state.send_to_client
    built: list[tuple] = []

    def spy(ws_ref, message):
        built.append((ws_ref, message))
        return real_send_to_client(ws_ref, message)

    monkeypatch.setattr(ws_state, "send_to_client", spy)
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]

    frame = {"type": "thread_changed"}
    ws_registry.broadcast_to_user("user-1", frame)
    await _delivered(socket, "an on-loop broadcast stopped reaching its socket")

    assert built == [(socket, frame)]
    assert socket.sent == [frame]
    submitted.assert_not_called()


@pytest.mark.asyncio
async def test_an_on_loop_socket_send_survives_a_closed_socket(ws_registry) -> None:
    """The swallowed send error stays swallowed — _schedule_send never raises."""

    class _DeadSocket:
        async def send_json(self, message: dict) -> None:
            raise RuntimeError("socket is closed")

    ws_registry._ws_user_clients["user-1"] = [_DeadSocket()]
    app_loop.capture(asyncio.get_running_loop())
    try:
        ws_registry.broadcast_to_user("user-1", {"type": "thread_changed"})
        await asyncio.sleep(0.05)
    finally:
        app_loop.clear()
