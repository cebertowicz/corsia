"""A broadcast made off the event loop still reaches the socket.

A write that commits on a database worker runs its after-commit broadcast on
that worker, where no loop is running. The socket write is the one place that
needs the loop, so it hands itself over; no broadcaster has to remember to.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import infra.app_loop as app_loop
from infra.state import ws as sockets


class _Socket:
    def __init__(self) -> None:
        self.sent: list[tuple[dict, int]] = []
        self.arrived = asyncio.Event()

    async def send_json(self, message: dict) -> None:
        self.sent.append((message, threading.get_ident()))
        self.arrived.set()


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setattr(sockets, "_ws_user_clients", {})
    monkeypatch.setattr(sockets, "_ws_thread_clients", {})
    return sockets


@pytest.mark.parametrize(
    "broadcast",
    [
        lambda message: sockets.broadcast_to_user("user-1", message),
        lambda message: sockets.broadcast_to_thread("thread-1", message),
        lambda message: sockets.broadcast_event("thread-1", "user-1", message),
        sockets.broadcast_all,
    ],
    ids=["to_user", "to_thread", "event", "all"],
)
async def test_a_broadcast_from_a_worker_thread_is_written_on_the_loop(clients, monkeypatch, broadcast):
    socket = _Socket()
    clients._ws_user_clients["user-1"] = [socket]
    clients._ws_thread_clients["thread-1"] = [socket]
    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())
    message = {"type": "turn_complete", "n": 1}

    await asyncio.to_thread(broadcast, message)
    await asyncio.wait_for(socket.arrived.wait(), timeout=5)

    assert socket.sent == [(message, threading.get_ident())]


async def test_document_open_send_from_a_worker_reaches_only_claiming_sessions(clients, monkeypatch):
    claiming = _Socket()
    hidden = _Socket()
    clients._ws_thread_clients["thread-1"] = [claiming, hidden]
    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())

    from infra.ws_session import session_id

    count = await asyncio.to_thread(
        sockets.send_to_thread_sessions,
        "thread-1",
        frozenset({session_id(claiming)}),
        {"type": "document_open_request"},
    )
    await asyncio.wait_for(claiming.arrived.wait(), timeout=5)

    assert count == 1
    assert [message for message, _thread in claiming.sent] == [{"type": "document_open_request"}]
    assert hidden.sent == []


async def test_broadcasts_from_one_worker_arrive_in_the_order_they_were_made(clients, monkeypatch):
    socket = _Socket()
    clients._ws_user_clients["user-1"] = [socket]
    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())

    def several() -> None:
        for n in range(5):
            sockets.broadcast_to_user("user-1", {"type": "artifacts_changed", "n": n})

    await asyncio.to_thread(several)
    for _ in range(200):
        if len(socket.sent) == 5:
            break
        await asyncio.sleep(0.01)

    assert [message["n"] for message, _thread in socket.sent] == [0, 1, 2, 3, 4]


def test_a_broadcast_with_no_live_loop_is_logged_not_raised(clients, monkeypatch):
    from loguru import logger

    socket = _Socket()
    clients._ws_user_clients["user-1"] = [socket]
    monkeypatch.setattr(app_loop, "_LOOP", None)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        sockets.broadcast_to_user("user-1", {"type": "plans_changed"})
    finally:
        logger.remove(sink)

    assert socket.sent == []
    assert any(
        "WS broadcast dropped, no application event loop" in line for line in lines
    )


def test_session_targeted_send_with_no_live_loop_returns_zero(clients, monkeypatch):
    """A dropped handoff must not be counted as an accepted document request."""
    socket = _Socket()
    clients._ws_thread_clients["thread-1"] = [socket]
    monkeypatch.setattr(app_loop, "_LOOP", None)

    assert sockets.send_to_thread_sessions(
        "thread-1", frozenset({"session-1"}), {"type": "document_open_request"}
    ) == 0
    assert socket.sent == []


async def test_session_targeted_send_counts_admitted_presence_targets(clients):
    """Request count describes the accepted presence snapshot, not socket delivery."""
    socket = _Socket()
    clients._ws_thread_clients["thread-1"] = [socket]

    count = sockets.send_to_thread_sessions(
        "thread-1", frozenset({"departed-session"}), {"type": "document_open_request"}
    )

    assert count == 1
    await asyncio.sleep(0)
    assert socket.sent == []


async def test_a_socket_that_fails_off_loop_does_not_stop_the_next_one(clients, monkeypatch):
    class Closed:
        async def send_json(self, message: dict) -> None:
            raise RuntimeError("socket closed")

    socket = _Socket()
    clients._ws_user_clients["user-1"] = [Closed(), socket]
    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())

    await asyncio.to_thread(sockets.broadcast_to_user, "user-1", {"type": "plans_changed"})
    await asyncio.wait_for(socket.arrived.wait(), timeout=5)

    assert [message for message, _thread in socket.sent] == [{"type": "plans_changed"}]


def test_a_loop_that_closes_after_the_check_is_logged_and_the_send_closed(clients, monkeypatch):
    """Shutdown can close the loop between submit's check and its hand-over."""
    from loguru import logger

    handed: list = []

    class ClosingLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, _callback, coroutine) -> None:
            handed.append(coroutine)
            raise RuntimeError("Event loop is closed")

    socket = _Socket()
    clients._ws_user_clients["user-1"] = [socket]
    monkeypatch.setattr(app_loop, "_LOOP", ClosingLoop())
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        sockets.broadcast_to_user("user-1", {"type": "plans_changed"})
    finally:
        logger.remove(sink)

    assert socket.sent == []
    assert any(
        "WS broadcast dropped, no application event loop" in line for line in lines
    )
    assert len(handed) == 1 and handed[0].cr_frame is None


def test_devices_update_schedules_one_type_only_broadcast(monkeypatch):
    scheduled: list[str] = []
    messages: list[dict] = []

    def schedule(callback, what):
        scheduled.append(what)
        callback()

    monkeypatch.setattr(sockets, "broadcast_from_any_thread", schedule)
    monkeypatch.setattr(sockets, "broadcast_all", messages.append)

    sockets.schedule_devices_updated()

    assert scheduled == ["devices_updated"]
    assert messages == [{"type": "devices_updated"}]
