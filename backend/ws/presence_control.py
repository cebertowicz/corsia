"""WebSocket ``presence`` frames and socket-close release.

A presence frame is state, not an input: it is handled inline by the frame
reader and never reaches the Turn queue, message admission, or any wake.
Frames carry no client time; the server stamps every transition. Every
failure here is logged and dropped, because presence must never break the
socket or block a message.

Frames::

    {"type": "presence", "surface": "chat", "thread_id": "…", "state": "foreground"}
    {"type": "presence", "surface": "chat", "thread_id": "…", "state": "background",
     "reason": "hidden"}        # reason optional; "hidden" starts the 30 s grace
    {"type": "presence", "surface": "chat", "thread_id": "…", "state": "idle",
     "idle_for_ms": 612000}
    {"type": "presence", "surface": "chat", "thread_id": "…",
     "state": "document_open_capability", "document_open": {"can_show": true,
     "visible_artifact_id": null}}
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import WebSocket
from loguru import logger

from infra import presence, state, thread_updates
from infra.database import get_db
from infra.db_async import run_in_db
from infra.time import utc_now
from infra.ws_session import session_id

MAX_IDLE_FOR = timedelta(days=1)
SWEEP_INTERVAL_SECONDS = 30

_release_tasks: set[asyncio.Task] = set()



def _parse_document_open_capability(data: dict) -> tuple[bool, str | None] | None:
    """Validate a memory-only delivery capability frame without changing presence."""
    document_open = data.get("document_open")
    if data.get("state") != "document_open_capability" or not isinstance(document_open, dict):
        return None
    can_show, visible_artifact_id = document_open.get("can_show"), document_open.get("visible_artifact_id")
    if not isinstance(can_show, bool) or (visible_artifact_id is not None and not isinstance(visible_artifact_id, str)):
        return None
    return can_show, visible_artifact_id


def _parse_release(data: dict) -> tuple[str, timedelta | None] | None:
    """Return the (reason, idle_for) a background or idle frame asks for."""
    if data.get("state") == "background":
        return ("hidden" if data.get("reason") == "hidden" else "navigated", None)
    idle_ms = data.get("idle_for_ms")
    if data.get("state") != "idle" or isinstance(idle_ms, bool) or not isinstance(idle_ms, int):
        return None
    return ("idle", min(timedelta(milliseconds=max(0, idle_ms)), MAX_IDLE_FOR))


def _run(fn, **kwargs):
    db = get_db()
    try:
        return fn(db, now=utc_now(), **kwargs)
    finally:
        db.close()


async def _apply(fn, **kwargs) -> None:
    """Run one record writer, then nudge each changed thread's update dot.

    The nudge follows the writer's commit on the event loop, where sockets
    live; a thread whose write failed is never nudged, so its dot stays on.
    """
    changed = await run_in_db(_run, fn, **kwargs) or []
    by_user: dict[str, list[str]] = {}
    for user_id, thread_id in changed:
        by_user.setdefault(user_id, []).append(thread_id)
    for user_id, thread_ids in by_user.items():
        thread_updates.notify_changed(user_id, list(dict.fromkeys(thread_ids)))


async def handle_presence(ws: WebSocket, user: dict, data: dict, *, owned_thread_fn) -> None:
    """Apply one presence frame from ``ws``; never raises."""
    thread_id = data.get("thread_id")
    surface = data.get("surface")
    if (not isinstance(thread_id, str) or not thread_id
            or surface not in presence.ACCEPTED_SURFACES):
        logger.debug(f"presence: ignored malformed frame {data!r}")
        return
    try:
        capability = _parse_document_open_capability(data)
        if data.get("state") == "document_open_capability":
            if capability is None:
                logger.debug(f"presence: ignored malformed frame {data!r}")
                return
            presence.update_document_open_capability(
                user_id=user["id"], thread_id=thread_id, session=session_id(ws),
                can_show=capability[0], visible_artifact_id=capability[1],
            )
            return
        if data.get("state") == "foreground":
            if await owned_thread_fn(user, thread_id) is None:
                await state.send_to_client(ws, {
                    "type": "error", "thread_id": thread_id,
                    "message": "not_authorized_for_session",
                })
                return
            await _apply(presence.claim, user_id=user["id"], surface=surface,
                         thread_id=thread_id, session=session_id(ws))
            return
        parsed = _parse_release(data)
        if parsed is None:
            logger.debug(f"presence: ignored malformed frame {data!r}")
            return
        reason, idle_for = parsed
        await _apply(presence.release, session=session_id(ws),
                     surface=surface, reason=reason, idle_for=idle_for)
    except Exception as exc:
        logger.warning(f"presence: frame for {thread_id} dropped: {exc}")


async def _release_socket(session: str) -> None:
    try:
        await _apply(presence.release_session, session=session)
    except Exception as exc:
        logger.warning(f"presence: release of closed socket {session} failed: {exc}")


def release_socket(ws: WebSocket) -> None:
    """Release every claim of a closing socket, in the background."""
    try:
        task = asyncio.get_running_loop().create_task(_release_socket(session_id(ws)))
    except RuntimeError:
        return
    _release_tasks.add(task)
    task.add_done_callback(_release_tasks.discard)


async def presence_sweep_loop() -> None:
    """Reconcile the record after startup, then advance visit timers every 30 s."""
    try:
        await run_in_db(_run, presence.reconcile_startup)
    except Exception as exc:
        logger.warning(f"presence: startup reconcile failed: {exc}")
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await _apply(presence.sweep)
        except Exception as exc:
            logger.warning(f"presence: sweep failed: {exc}")
