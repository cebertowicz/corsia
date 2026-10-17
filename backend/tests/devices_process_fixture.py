"""Isolated real backend used by test_devices_process_lifecycle (never installed)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket

import psutil
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.responses import StreamingResponse


def main() -> None:
    # Refuse ambient/default state before importing any CORSIA module.
    assert os.environ["CORSIA_TEST_MODE"] == "1"
    assert os.environ["CORSIA_ANALYTICS_DISABLE"] == "1"
    workspace = Path(os.environ["CORSIA_WORKSPACE"]).resolve()
    state = Path(os.environ["CORSIA_STATE_DIR"]).resolve()
    assert workspace != state and workspace.is_dir() and state.is_dir()

    from infra import config, devices_listener
    import main as backend
    from core.devices_runtime import runtime

    assert config.WORKSPACE.resolve() == workspace
    assert config.STATE_ROOT.resolve() == state
    assert config.DB_PATH.resolve().is_relative_to(state)
    print("I9_IDENTITY " + json.dumps({
        "workspace": str(config.WORKSPACE), "state_root": str(config.STATE_ROOT),
        "db_path": str(config.DB_PATH), "pid": os.getpid(),
    }), flush=True)

    def loopback_socket():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(socket.SOMAXCONN)
        sock.setblocking(False)
        return sock

    def prebind():
        sock = loopback_socket()
        return sock, sock.getsockname()[1], None

    # Only address selection changes: real TLS/Uvicorn/DNS-SD owners remain.
    devices_listener._prebind = prebind

    @asynccontextmanager
    async def lifespan(app):
        async with backend.app.router.lifespan_context(backend.app):
            print("I9_READY", flush=True)
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/__i9/counts")
    async def counts():
        return {
            "fds": psutil.Process().num_fds(),
            "tasks": len([task for task in asyncio.all_tasks() if not task.done()]),
            "threads": psutil.Process().num_threads(),
        }

    @app.put("/__i9/devices/{enabled}")
    async def switch(enabled: bool):
        if enabled:
            assert await runtime.enable(app)
            while not runtime.listener.server.started:
                await asyncio.sleep(0.001)
            return {"port": runtime.listener.port}
        await runtime.disable()
        return {"port": None}

    @app.get("/__i9/events")
    async def events():
        async def stream():
            yield b"data: ready\n\n"
            await asyncio.Event().wait()
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/__i9/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ready")
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass

    app.mount("/", backend.app)
    sock = loopback_socket()
    print("I9_PORT " + str(sock.getsockname()[1]), flush=True)
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="info", lifespan="on", timeout_graceful_shutdown=2,
    ))
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
