"""Authenticated relay between the browser pane and agent-browser's loopback stream."""
from __future__ import annotations

import asyncio
import json

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from host_access import browser_pane
from infra.database import get_db
from infra.queries import get_thread_for_user
from ws.thread_control import authenticate_ws, cleanup_ws


async def browser_stream_endpoint(ws: WebSocket, thread_id: str) -> None:
    """Relay one owned thread's private stream without revealing its port."""
    await ws.accept()
    user = await authenticate_ws(ws)
    if not user:
        return
    db = get_db()
    try:
        if not get_thread_for_user(thread_id, user["id"], db=db):
            await ws.close(code=1008)
            return
    finally:
        db.close()
    port = await browser_pane.stream_port(thread_id)
    if port is None:
        await ws.send_json({"type": "browser_state", "state": "no_browser_yet"})
        await ws.close()
        cleanup_ws(user["id"], ws)
        return
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as stream:
            await _relay(ws, stream)
    except WebSocketDisconnect:
        pass
    finally:
        cleanup_ws(user["id"], ws)


async def _relay(pane: WebSocket, stream) -> None:
    # Native stream clients normally begin a screencast on connect. Starting one
    # explicitly makes the frame lifecycle deterministic across daemon versions.
    await stream.send(json.dumps({"type": "screencast_start"}))

    async def from_stream() -> None:
        async for message in stream:
            await pane.send_text(message)

    async def from_pane() -> None:
        while True:
            message = await pane.receive_text()
            if not browser_pane.has_user_control(_thread_id_from_path(pane)):
                continue
            data = json.loads(message)
            if data.get("type") in {"input_mouse", "input_keyboard"}:
                await stream.send(message)

    done, pending = await asyncio.wait(
        {asyncio.create_task(from_stream()), asyncio.create_task(from_pane())},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def _thread_id_from_path(ws: WebSocket) -> str:
    return str(ws.path_params["thread_id"])
