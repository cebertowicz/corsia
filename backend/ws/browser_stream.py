"""Authenticated relay between the browser pane and agent-browser's loopback stream."""
from __future__ import annotations

import asyncio
import json
import secrets

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from host_access import browser_control, browser_pane
from infra.auth import authenticate
from infra.database import get_db
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user


async def browser_stream_endpoint(ws: WebSocket, thread_id: str) -> None:
    """Relay one owned thread's private stream without joining chat socket state."""
    await ws.accept()
    user = await _authenticate_pane_stream(ws)
    if user is None:
        return
    if not _user_owns_thread(thread_id, user["id"]):
        await ws.close(code=1008)
        return
    outcome = await browser_pane.stream_port(thread_id)
    if outcome.no_session:
        await ws.send_json({"type": "browser_state", "state": "no_browser_yet"})
        await ws.close()
        return
    if outcome.error:
        await ws.send_json({"type": "browser_state", "state": "error", "reason": outcome.error})
        await ws.close()
        return
    try:
        async with websockets.connect(f"ws://127.0.0.1:{outcome.port}", max_size=None) as stream:
            await _relay(ws, stream, thread_id, user["id"])
    except WebSocketDisconnect:
        pass
    except OSError:
        await ws.send_json({"type": "browser_state", "state": "error", "reason": "browser stream connection failed"})
        await ws.close()


async def _authenticate_pane_stream(ws: WebSocket) -> dict | None:
    """Authenticate the first pane message without chat presence registration."""
    try:
        message = await asyncio.wait_for(ws.receive_json(), timeout=10)
        token = message.get("token", "") if isinstance(message, dict) else ""
        user = authenticate(token)
    except WebSocketDisconnect:
        return None  # The pane left first; there is no socket left to close.
    except (TimeoutError, ValueError):
        user = None
    if not user:
        await ws.close(code=1008)
        return None
    return user


def _user_owns_thread(thread_id: str, user_id: str) -> bool:
    db = get_db()
    try:
        return bool(get_thread_for_user(thread_id, user_id, db=db))
    finally:
        db.close()


async def _relay(pane: WebSocket, stream, thread_id: str, user_id: str) -> None:
    """Proxy frames and accept input only from this connection's control holder."""
    connection_token = secrets.token_urlsafe(18)

    async def send_control() -> None:
        await pane.send_json({
            "type": "control",
            "has_control": browser_control.holds_control(thread_id, connection_token),
        })

    async def from_stream() -> None:
        async for message in stream:
            await pane.send_text(message)

    async def from_pane() -> None:
        while True:
            message = await pane.receive_text()
            try:
                data = json.loads(message)
            except (TypeError, ValueError):
                continue
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "take_over":
                if browser_control.take_control(thread_id, connection_token):
                    emit_product_event("browser_pane", "take_over", thread_id=thread_id, user_id=user_id)
                await send_control()
            elif kind == "hand_back":
                if browser_control.hand_back(thread_id, connection_token):
                    emit_product_event("browser_pane", "hand_back", thread_id=thread_id, user_id=user_id)
                await send_control()
            elif (kind in {"input_mouse", "input_keyboard"}
                  and browser_control.holds_control(thread_id, connection_token)):
                await stream.send(message)

    try:
        await stream.send(json.dumps({"type": "screencast_start"}))
        await send_control()
        done, pending = await asyncio.wait(
            {asyncio.create_task(from_stream()), asyncio.create_task(from_pane())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        if browser_control.hand_back(thread_id, connection_token):
            emit_product_event("browser_pane", "hand_back", thread_id=thread_id, user_id=user_id)
