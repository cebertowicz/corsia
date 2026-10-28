"""WebSocket state — client tracking and broadcast utilities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from infra.ws_session import session_id

__all__ = [
    "_ws_thread_clients", "_ws_user_clients",
    "broadcast_to_thread", "send_to_thread_sessions", "broadcast_event", "broadcast_to_user",
    "broadcast_all", "owner_broadcast",
    "send_to_client",
    "notify_models_changed", "notify_devices_updated", "schedule_devices_updated", "notify_connected_apps_updated",
    "CROSS_TAB_EVENTS",
]

_ws_thread_clients: dict[str, list] = {}
_ws_user_clients: dict[str, list] = {}


def _observe_send_task(task: asyncio.Task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, RuntimeError):
        pass


def _send_lock(ws_ref) -> asyncio.Lock:
    lock = getattr(ws_ref, "_corsia_send_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(ws_ref, "_corsia_send_lock", lock)
    return lock


async def _send_to_client(ws_ref, message: dict) -> None:
    async with _send_lock(ws_ref):
        await ws_ref.send_json(message)


def send_to_client(ws_ref, message: dict) -> asyncio.Task[None]:
    """Reserve one ordered socket write and return its completion task."""
    return asyncio.create_task(_send_to_client(ws_ref, message))


def _has_running_loop() -> bool:
    """Whether the calling thread is inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _submit_broadcast_to_app_loop(broadcast, *args, **kwargs) -> bool:
    """Give the application loop one complete off-loop broadcast fan-out.

    A DB worker's after-commit nudge runs on a thread with no running loop. The
    client registries belong to the loop, so the whole fan-out hops there before
    any registry is read; a connect or disconnect cannot change one mid-iteration.
    ``call_soon_threadsafe`` is FIFO, so one thread's broadcasts reach the loop in
    the order they were issued. A broadcast with no loop to take it is a drop,
    logged rather than raised into the caller's after-commit hook.
    """
    import infra.app_loop as app_loop

    async def _broadcast() -> None:
        broadcast(*args, **kwargs)

    try:
        app_loop.submit(_broadcast())
    except app_loop.AppLoopUnavailable as exc:
        from loguru import logger

        logger.warning("WS broadcast dropped, no application event loop to deliver it: {}", exc)
        return False
    return True


def _schedule_send(ws_ref, message: dict) -> None:
    """Start one socket write; callers are on the loop (off-loop broadcasts hop first)."""
    try:
        task = send_to_client(ws_ref, message)
    except (RuntimeError, TypeError):
        return
    task.add_done_callback(_observe_send_task)


def broadcast_to_thread(thread_id: str, message: dict, exclude_ws=None):
    """Best-effort broadcast a message to any WS clients viewing a session."""
    if not _has_running_loop():
        _submit_broadcast_to_app_loop(broadcast_to_thread, thread_id, message, exclude_ws=exclude_ws)
        return
    for ws_ref in _ws_thread_clients.get(thread_id, []):
        if ws_ref is exclude_ws:
            continue
        _schedule_send(ws_ref, message)


def send_to_thread_sessions(thread_id: str, sessions: frozenset[str], message: dict) -> int:
    """Schedule a frame only for the claiming sessions of one thread.

    The return value is the number of presence targets whose handoff the app
    loop accepted, not a count of socket delivery. Off-loop callers hand the
    registry read and socket writes to the application loop, matching every
    other broadcast path.
    """
    if not _has_running_loop():
        accepted = _submit_broadcast_to_app_loop(
            send_to_thread_sessions, thread_id, sessions, message
        )
        return len(sessions) if accepted else 0
    for ws_ref in _ws_thread_clients.get(thread_id, []):
        if session_id(ws_ref) not in sessions:
            continue
        _schedule_send(ws_ref, message)
    return len(sessions)


# Every event that ENDS a turn must reach all of a user's connected clients so
# their drawer spinners are truthful off-thread / cross-device — not just the
# socket viewing the session. ``error`` is a turn terminal (an errored turn emits
# no ``turn_complete``); omitting it left off-thread spinners stuck until reconnect.
CROSS_TAB_EVENTS = frozenset({
    "turn_complete", "turn_cancelled", "turn_interrupted", "error",
    "session_updated",
})


def broadcast_event(thread_id: str, user_id: str, message: dict, exclude_ws=None):
    """Smart broadcast: session-scoped + user-scoped for cross-tab events."""
    if not _has_running_loop():
        _submit_broadcast_to_app_loop(
            broadcast_event, thread_id, user_id, message, exclude_ws=exclude_ws
        )
        return
    broadcast_to_thread(thread_id, message, exclude_ws=exclude_ws)

    msg_type = message.get("type")
    if msg_type in CROSS_TAB_EVENTS or msg_type == "turn_activity":
        for ws_ref in tuple(_ws_user_clients.get(user_id, ())):
            if ws_ref is exclude_ws:
                continue
            if ws_ref in _ws_thread_clients.get(thread_id, []):
                continue
            _schedule_send(ws_ref, message)


def owner_broadcast(user_id: str):
    """A broadcast callable bound to a thread's owner — the single cross-tab path
    for CC/Codex runtimes.

    The provider runtimes bake a broadcast callable into their long-lived process
    event pump at spawn, so they cannot pass the owner per-event. This closure
    captures the owner once and routes every turn event through
    ``broadcast_event``: session-scoped to active viewers PLUS the owner's other
    clients for turn-lifecycle terminals (so off-thread drawer spinners are
    truthful), deduped against the active socket. It replaces the session-scoped
    ``broadcast_to_thread`` default that reached only sockets viewing the thread.
    """
    def _bcast(thread_id: str, message: dict) -> None:
        broadcast_event(thread_id, user_id, message)
    return _bcast


def broadcast_to_user(user_id: str, message: dict, exclude_ws=None):
    """Best-effort broadcast a message to ALL of a user's WS connections."""
    if not _has_running_loop():
        _submit_broadcast_to_app_loop(broadcast_to_user, user_id, message, exclude_ws=exclude_ws)
        return
    for ws_ref in _ws_user_clients.get(user_id, []):
        if ws_ref is exclude_ws:
            continue
        _schedule_send(ws_ref, message)


def broadcast_all(message: dict):
    """Best-effort broadcast to every connected WS client across all users/sessions.

    Used for system-global events (e.g. model catalog updates). Dedupes by
    socket identity so a client connected to both a user bucket and a session
    bucket only receives one copy.
    """
    if not _has_running_loop():
        _submit_broadcast_to_app_loop(broadcast_all, message)
        return
    seen: set[int] = set()
    for bucket in (_ws_user_clients, _ws_thread_clients):
        for ws_refs in tuple(bucket.values()):
            for ws_ref in tuple(ws_refs):
                if id(ws_ref) in seen:
                    continue
                seen.add(id(ws_ref))
                _schedule_send(ws_ref, message)


def notify_models_changed() -> None:
    """Broadcast catalog changes and resume any now-runnable held FIFO heads."""
    broadcast_all({"type": "models_updated"})
    from infra.unavailable_model_recovery import schedule_unavailable_model_recovery

    schedule_unavailable_model_recovery()


def notify_devices_updated() -> None:
    """Tell every connected client to re-read its Devices state."""
    broadcast_all({"type": "devices_updated"})


def notify_connected_apps_updated() -> None:
    """Tell every connected client to re-read Connected apps (a mail-host check ended)."""
    broadcast_all({"type": "connected_apps_updated"})


def broadcast_from_any_thread(broadcast: Callable[[], None], what: str) -> None:
    """Run ``broadcast`` on the application loop, from a DB worker's after-commit included.

    One hop for the whole notification instead of one per socket. The broadcast
    now finds the application loop on its own, but this still belongs here: it
    submits the catalog frame and the held-model recovery as a single unit, so a
    worker's after-commit hands the loop both parts together.
    """
    import infra.app_loop as app_loop

    async def _on_loop() -> None:
        broadcast()

    try:
        app_loop.submit(_on_loop())
    except app_loop.AppLoopUnavailable:
        from loguru import logger

        logger.warning(f"{what} not sent: no live event loop; clients reload on reconnect")


def schedule_models_changed() -> None:
    """``notify_models_changed`` from any thread."""
    broadcast_from_any_thread(notify_models_changed, "models_updated")


def schedule_devices_updated() -> None:
    """``notify_devices_updated`` from the pairing state owner or a DB worker."""
    broadcast_from_any_thread(notify_devices_updated, "devices_updated")
