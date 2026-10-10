from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket
from loguru import logger

from infra import state
from infra.auth import authenticate
from core.turn_core import TurnIdentity
from core.turn_core import active_turn_thread_ids, active_turns_for_owner
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from llm.resolution import ModelUnavailableError
from ws.presence_control import release_socket


OwnedThread = Callable[[dict, str], Awaitable[object | None]]


def active_turn_ids() -> set[str]:
    """Canonical thread ids with a durable generation in progress."""
    return active_turn_thread_ids()


async def authenticate_ws(ws: WebSocket) -> dict | None:
    try:
        auth_msg = await asyncio_wait_for_json(ws, timeout=10)
        tok = auth_msg.get("token", "")
        user = authenticate(tok)
        if not user:
            await state.send_to_client(
                ws, {"type": "error", "message": "Invalid or expired session"}
            )
            await ws.close()
            return None
        active_turns = active_turns_for_owner(user["id"])
        await state.send_to_client(ws, {
            "type": "auth_ok",
            "user": {"id": user["id"], "name": user["name"]},
            "active_turns": active_turns,
        })
        user_id = user["id"]
        if user_id not in state._ws_user_clients:
            state._ws_user_clients[user_id] = []
        state._ws_user_clients[user_id].append(ws)
        return user
    except Exception as exc:
        logger.warning(f"WS connect handshake failed: {exc}")
        try:
            await ws.close()
        except (RuntimeError, ConnectionError):
            pass
        return None


async def asyncio_wait_for_json(ws: WebSocket, *, timeout: float) -> dict:
    import asyncio
    return await asyncio.wait_for(ws.receive_json(), timeout=timeout)


def cleanup_ws(user_id: str, ws: WebSocket) -> None:
    release_socket(ws)
    if user_id in state._ws_user_clients:
        try:
            state._ws_user_clients[user_id].remove(ws)
            if not state._ws_user_clients[user_id]:
                del state._ws_user_clients[user_id]
        except ValueError:
            pass
    for sid in list(state._ws_thread_clients.keys()):
        clients = state._ws_thread_clients.get(sid, [])
        if ws in clients:
            clients.remove(ws)
            if not clients:
                del state._ws_thread_clients[sid]
    for info in state._active_thread_models.values():
        if info.get("ws_connected"):
            info["ws_connected"] = False


async def _send_subagent_meta(
    ws: WebSocket,
    row: Any,
    thread_id: str,
    *,
    live: bool,
) -> None:
    from infra.execution_class import is_subagent, provider_label

    if not is_subagent(row):
        return
    model = row["model"] if "model" in row.keys() else None
    try:
        provider = provider_label(model)
    except ModelUnavailableError:
        provider = "unavailable"
    parent_id = row["spawned_by"] or row["parent_thread_id"]
    try:
        await state.send_to_client(ws, {
            "type": "subagent_meta",
            "thread_id": thread_id,
            "parent_thread_id": parent_id,
            "name": row["title"],
            "provider": provider,
            "live": live,
            "model": model,
        })
    except Exception as exc:
        logger.warning(f"subagent_meta send failed for {thread_id}: {exc}")


async def handle_subscribe(
    ws: WebSocket,
    user: dict,
    thread_id: str,
    *,
    owned_thread_fn: OwnedThread,
) -> None:
    """Subscribe ``ws`` to ``thread_id``: send the turn-state snapshot, and for a
    subagent (``is_subagent`` / ``spawned_by`` set) also emit its ``subagent_meta``."""
    if not thread_id:
        return
    row = await owned_thread_fn(user, thread_id)
    if row is None:
        try:
            await state.send_to_client(ws, {
                "type": "error", "thread_id": thread_id,
                "message": "not_authorized_for_session",
            })
        except (RuntimeError, ConnectionError, AttributeError):
            pass
        return

    state._ws_thread_clients.setdefault(thread_id, [])
    if ws not in state._ws_thread_clients[thread_id]:
        state._ws_thread_clients[thread_id].append(ws)

    from infra.state import turn_buffers as _stb
    active_turns = active_turns_for_owner(
        user["id"],
        thread_id=thread_id,
    )
    turn_id = active_turns[0]["turn_id"] if active_turns else None
    turn_order = active_turns[0]["turn_order"] if active_turns else None
    identity = (
        TurnIdentity(turn_id, turn_order)
        if turn_id is not None and turn_order is not None
        else None
    )
    snapshot = _stb.snapshot(thread_id, identity) if identity else None
    # Same visible-root contract as the auth snapshot: null when there is no
    # live Turn or the lineage is unresolvable.
    visible_root_thread_id = (
        active_turns[0].get("visible_root_thread_id") if active_turns else None
    )
    started_at = active_turns[0].get("started_at") if active_turns else None
    try:
        await state.send_to_client(ws, {
            "type": "session_snapshot",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "turn_order": turn_order,
            "visible_root_thread_id": visible_root_thread_id,
            "started_at": started_at,
            "state": snapshot,
        })
    except Exception as exc:
        logger.warning(f"snapshot send failed for {thread_id}: {exc}")

    await _send_subagent_meta(ws, row, thread_id, live=turn_id is not None)


def handle_unsubscribe(ws: WebSocket, thread_id: str) -> None:
    if not thread_id:
        return
    clients = state._ws_thread_clients.get(thread_id)
    if not clients:
        return
    try:
        clients.remove(ws)
    except ValueError:
        return
    if not clients:
        state._ws_thread_clients.pop(thread_id, None)


async def owned_thread(user: dict, thread_id: str):
    """Fetch a session row (``SELECT *``) scoped to ``user``, or None if missing/unowned."""
    if not thread_id:
        return None

    def _check():
        db = get_db()
        try:
            return get_thread_for_user(thread_id, user["id"], db=db)
        finally:
            db.close()

    return await run_in_db(_check)
