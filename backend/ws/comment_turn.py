"""WebSocket ``comment_turn`` frames: a person's page holding a comment thread's writer turn.

A frame is state, not an input: it is handled inline by the frame reader and never reaches the
Turn queue. Every failure is logged and dropped, because the turn must never break the socket.

Frames::

    {"type": "comment_turn", "comment_id": "…", "on": true}   # on the thread; renewed each minute
    {"type": "comment_turn", "on": false}                     # left it (sent, cleared, hidden, idle)

A page that stops renewing loses the turn after ``writer_turns.LEASE_SECONDS``; a closed socket
loses it at once. Whenever a thread frees, the first agent waiting on it is woken.
"""

from __future__ import annotations

import asyncio

from fastapi import WebSocket
from loguru import logger

from doceditor import agent_comment_writes, comments as store, writer_line, writer_turns
from infra.auth import may_access_artifact
from infra.database import get_db
from infra.db_async import run_in_db
from registry import models as registry_models

FRAME_TYPE = "comment_turn"

_leases: dict[object, asyncio.TimerHandle] = {}
_tasks: set[asyncio.Task] = set()


def _root(comment_id: str, user_id: str) -> tuple[str, str] | None:
    """The thread root and document of a comment the user may reach."""
    db = get_db()
    try:
        comment = store.get_comment(comment_id, db=db)
        if comment is None:
            return None
        artifact = registry_models.get_artifact(comment["artifact_id"], db=db)
        if artifact is None or not may_access_artifact(artifact["parent_thread_id"], user_id, db=db):
            return None
        return str(comment.get("parent_id") or comment["id"]), str(comment["artifact_id"])
    finally:
        db.close()


def _forget_lease(page: object) -> None:
    handle = _leases.pop(page, None)
    if handle is not None:
        handle.cancel()


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _serve(freed: list[str]) -> None:
    try:
        await agent_comment_writes.serve_freed(freed)
    except Exception as exc:
        logger.warning(f"comment_turn: serving {freed} failed: {exc}")


def _expire(page: object) -> None:
    _leases.pop(page, None)
    freed = writer_turns.release(page)
    if freed:
        _spawn(_serve(freed))


async def handle_frame(ws: WebSocket, user: dict, data: dict) -> None:
    """Apply one ``comment_turn`` frame from ``ws``; never raises."""
    comment_id, on = data.get("comment_id"), data.get("on")
    if not isinstance(on, bool) or (on and not isinstance(comment_id, str)):
        logger.debug(f"comment_turn: ignored malformed frame {data!r}")
        return
    try:
        if not on:
            _forget_lease(ws)
            await _serve(writer_turns.release(ws))
            return
        found = await run_in_db(_root, comment_id, user["id"])
        if found is None:
            return
        root_id, artifact_id = found
        freed = writer_turns.take(ws, root_id, writer_turns.PersonHold(
            user_id=user["id"], name=str(user.get("name") or "someone"), artifact_id=artifact_id))
        _forget_lease(ws)
        _leases[ws] = asyncio.get_running_loop().call_later(writer_turns.LEASE_SECONDS, _expire, ws)
        await run_in_db(writer_line.publish, root_id, artifact_id, user_ids={user["id"]})
        await _serve(freed)
    except Exception as exc:
        logger.warning(f"comment_turn: frame for {comment_id} dropped: {exc}")


def disconnect(ws: WebSocket) -> None:
    """A closed socket's page leaves its thread at once."""
    _forget_lease(ws)
    freed = writer_turns.release(ws)
    if freed:
        try:
            _spawn(_serve(freed))
        except RuntimeError:
            return
