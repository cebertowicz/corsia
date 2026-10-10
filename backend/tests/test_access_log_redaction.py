"""Access logs must not retain query-string credential values."""

from __future__ import annotations

import asyncio
import logging
import socket

import httpx
import pytest
import uvicorn


class _AccessRecords(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


async def _serve_app():
    import main

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(
        main.app,
        host="127.0.0.1",
        lifespan="off",
    ))
    task = asyncio.create_task(server._serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    return server, task, sock.getsockname()[1]


async def _stop_server(server, task) -> None:
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_uvicorn_access_log_drops_query_values_for_api_and_download_routes():
    import main

    marker = "synthetic-query-marker"
    main.install_access_log_redaction()
    server, task, port = await _serve_app()
    access_logger = logging.getLogger("uvicorn.access")
    previous_handlers = access_logger.handlers[:]
    previous_level = access_logger.level
    previous_propagate = access_logger.propagate
    records = _AccessRecords()
    access_logger.handlers = [records]
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            api = await client.get("/api/settings", params={"token": marker})
            download = await client.get(
                "/api/uploads/missing-user/missing-file/missing-name",
                params={"token": marker},
            )
    finally:
        await _stop_server(server, task)
        access_logger.handlers = previous_handlers
        access_logger.setLevel(previous_level)
        access_logger.propagate = previous_propagate

    lines = "\n".join(records.messages)
    assert marker not in lines
    assert 'GET /api/settings HTTP/1.1" ' + str(api.status_code) in lines
    assert 'GET /api/uploads/missing-user/missing-file/missing-name HTTP/1.1" ' + str(download.status_code) in lines
