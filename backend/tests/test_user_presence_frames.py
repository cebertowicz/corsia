"""Presence frames on the real WebSocket reader (#2004, design §6.12 test 1).

A presence frame is state, not input: it must never reach the Turn queue,
message admission or the context journal, and entering a thread must never
start a Turn.
"""

from __future__ import annotations

import asyncio

from fastapi import WebSocketDisconnect

import ws as ws_endpoint
from infra import presence
from infra.ws_session import session_id
from infra.thread_liveness import read_thread_liveness
from ws import presence_control
from ws.thread_control import cleanup_ws
from tests.presence_helpers import USER, new_thread, pdb, row

__all__ = ["pdb"]


class _Socket:
    """A WebSocket that yields scripted frames, then disconnects."""

    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.sent: list[dict] = []

    async def receive_json(self) -> dict:
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _count(db, table: str, thread_id: str | None = None) -> int:
    if thread_id is None:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (thread_id,),
    ).fetchone()[0]


def _read(frames: list[dict], user: dict) -> tuple[_Socket, list]:
    """Run the real frame reader over ``frames``; return the socket and queue."""
    socket = _Socket(frames)
    queue: asyncio.Queue = asyncio.Queue()
    asyncio.run(ws_endpoint._receive_frames(socket, user, queue))
    queued = []
    while not queue.empty():
        queued.append(queue.get_nowait())
    return socket, queued


def test_entering_a_thread_never_starts_a_turn(pdb):
    thread = new_thread(pdb, "enter-idle")
    journal_before = _count(pdb, "context_change_journal")

    _socket, queued = _read([{
        "type": "presence", "surface": "chat",
        "thread_id": thread, "state": "foreground",
    }], {"id": USER})

    assert row(pdb, thread)["present"] == 1  # the frame was applied
    assert queued == [None]  # only the disconnect sentinel reached the queue
    assert _count(pdb, "active_turns", thread) == 0
    assert _count(pdb, "messages", thread) == 0
    assert _count(pdb, "context_change_journal") == journal_before
    assert read_thread_liveness(thread, turn_active=False, db=pdb).queued_inputs == 0


def test_frame_for_another_users_thread_is_refused(pdb):
    thread = new_thread(pdb, "not-yours")
    socket, queued = _read([{
        "type": "presence", "surface": "chat",
        "thread_id": thread, "state": "foreground",
    }], {"id": "someone-else"})

    assert row(pdb, thread) is None
    assert queued == [None]
    assert socket.sent == [{
        "type": "error", "thread_id": thread, "message": "not_authorized_for_session",
    }]


def test_malformed_frames_are_dropped_without_breaking_the_socket(pdb):
    thread = new_thread(pdb, "malformed")
    frames = [
        {"type": "presence", "thread_id": thread, "state": "foreground"},
        {"type": "presence", "surface": "documents", "thread_id": thread,
         "state": "foreground"},
        {"type": "presence", "surface": "chat", "thread_id": thread,
         "state": "idle", "idle_for_ms": "600000"},
        {"type": "presence", "surface": "chat", "state": "foreground"},
    ]

    socket, queued = _read(frames, {"id": USER})

    assert queued == [None]  # every frame was read; the loop ended on disconnect
    assert row(pdb, thread) is None
    assert socket.sent == []


def test_live_view_reports_current_claims(pdb):
    thread = new_thread(pdb, "snapshot")
    first = _Socket([])
    second = _Socket([])

    async def _scenario() -> None:
        for socket in (first, second):
            await presence_control.handle_presence(
                socket, {"id": USER},
                {"type": "presence", "surface": "chat", "thread_id": thread,
                 "state": "foreground"},
                owned_thread_fn=ws_endpoint._owned_thread,
            )
        assert presence.live_view(USER, thread) == ("here", None)
        await presence_control.handle_presence(
            second, {"id": USER},
            {"type": "presence", "surface": "chat", "thread_id": thread,
             "state": "background"},
            owned_thread_fn=ws_endpoint._owned_thread,
        )

    asyncio.run(_scenario())
    assert presence.live_view(USER, thread) == ("here", None)


def test_document_open_snapshot_filters_claims_without_changing_presence(pdb):
    thread = new_thread(pdb, "document-open-capability")
    desktop, compact = _Socket([]), _Socket([])

    async def _scenario() -> None:
        for socket in (desktop, compact):
            await presence_control.handle_presence(
                socket, {"id": USER},
                {"type": "presence", "surface": "chat", "thread_id": thread, "state": "foreground"},
                owned_thread_fn=ws_endpoint._owned_thread,
            )
        for socket, can_show, visible_artifact_id in (
            (desktop, True, None),
            (compact, False, "artifact-a"),
        ):
            await presence_control.handle_presence(
                socket, {"id": USER},
                {
                    "type": "presence", "surface": "chat", "thread_id": thread,
                    "state": "document_open_capability",
                    "document_open": {"can_show": can_show, "visible_artifact_id": visible_artifact_id},
                },
                owned_thread_fn=ws_endpoint._owned_thread,
            )

    asyncio.run(_scenario())

    with presence._LOCK:
        presence._visits[(USER, presence.CHAT_SURFACE, "unstamped")] = presence._Visit()
    assert presence.document_open_snapshot(USER, "unstamped", "artifact-b").verdict == "unknown"

    snapshot = presence.document_open_snapshot(USER, thread, "artifact-b")
    assert snapshot.verdict == "here"
    assert snapshot.eligible_session_ids == frozenset({session_id(desktop)})
    assert not snapshot.already_open
    target = presence.document_open_snapshot(USER, thread, "artifact-a")
    assert target.eligible_session_ids == frozenset({session_id(desktop)})
    assert target.already_open


def test_document_open_snapshot_links_a_background_target_when_another_document_is_visible(pdb):
    thread = new_thread(pdb, "document-open-background")
    desktop = _Socket([])

    async def _scenario() -> None:
        await presence_control.handle_presence(
            desktop, {"id": USER},
            {"type": "presence", "surface": "chat", "thread_id": thread, "state": "foreground"},
            owned_thread_fn=ws_endpoint._owned_thread,
        )
        await presence_control.handle_presence(
            desktop, {"id": USER},
            {
                "type": "presence", "surface": "chat", "thread_id": thread,
                "state": "document_open_capability",
                "document_open": {"can_show": False, "visible_artifact_id": "artifact-a"},
            },
            owned_thread_fn=ws_endpoint._owned_thread,
        )

    asyncio.run(_scenario())

    snapshot = presence.document_open_snapshot(USER, thread, "artifact-b")
    assert snapshot.eligible_session_ids == frozenset()
    assert not snapshot.already_open
    assert presence.document_open_snapshot(USER, thread, "artifact-a").already_open


def test_socket_cleanup_releases_its_claims(pdb):
    thread = new_thread(pdb, "cleanup")
    socket = _Socket([])

    async def _scenario() -> None:
        await presence_control.handle_presence(
            socket, {"id": USER},
            {"type": "presence", "surface": "chat", "thread_id": thread,
             "state": "foreground"},
            owned_thread_fn=ws_endpoint._owned_thread,
        )
        assert presence.live_view(USER, thread) == ("here", None)
        cleanup_ws(USER, socket)
        await asyncio.gather(*presence_control._release_tasks)

    asyncio.run(_scenario())

    released = row(pdb, thread)
    assert released["exit_pending_reason"] == "disconnected"
    assert released["exit_pending_at"] is not None
    assert presence.live_view(USER, thread)[0] == "away"
