"""The node-usage rebuild runs before the server takes a request or starts a turn.

``core.node_usage_groups.ensure`` holds the write lock once per rebuild batch.
That is safe only because it runs where the schema migrations run: inside
``init_db``, which the lifespan calls before it reconciles interrupted turns,
starts runtime services or hands control to the server, and uvicorn binds the
port only after the lifespan's startup returns. Each link is pinned here.
"""

from __future__ import annotations

import asyncio
import socket

import uvicorn
from fastapi import FastAPI

import app_lifecycle
from test_app_lifecycle_shutdown import _isolate_lifespan  # noqa: F401 - shared lifespan harness


def test_init_db_runs_the_rebuild_after_the_schema_and_before_returning(monkeypatch):
    import core.node_usage_groups as groups
    from infra import database

    order: list[str] = []
    real_schema = database.initialize_schema

    def schema(conn):
        order.append("schema")
        return real_schema(conn)

    monkeypatch.setattr(database, "initialize_schema", schema)
    monkeypatch.setattr(groups, "ensure", lambda conn: order.append("ensure"))
    database.init_db()
    order.append("returned")
    assert order == ["schema", "ensure", "returned"]


async def test_lifespan_finishes_init_db_before_any_turn_service_or_request(monkeypatch):
    from automation import restart_startup

    _isolate_lifespan(monkeypatch)
    order: list[str] = []
    real_reconcile = restart_startup.run_startup_reconcile
    real_services = app_lifecycle._start_runtime_services

    async def reconcile(logger):
        order.append("reconcile interrupted turns")
        return await real_reconcile(logger)

    async def services():
        order.append("start runtime services")
        return await real_services()

    monkeypatch.setattr(app_lifecycle.app_startup_init, "init_db", lambda: order.append("init_db"))
    monkeypatch.setattr(restart_startup, "run_startup_reconcile", reconcile)
    monkeypatch.setattr(app_lifecycle, "_start_runtime_services", services)

    async with app_lifecycle.lifespan(FastAPI()):
        order.append("serving")

    assert order == ["init_db", "reconcile interrupted turns", "start runtime services", "serving"]


async def test_uvicorn_binds_no_socket_until_lifespan_startup_returns():
    """``main.py`` serves through ``uvicorn.run(app, host, port)``: nothing listens during startup."""
    from contextlib import asynccontextmanager

    listening_during_startup: list[int] = []
    server: uvicorn.Server

    @asynccontextmanager
    async def lifespan(_app):
        # ``servers`` is assigned only once ``create_server`` has bound the port.
        listening_during_startup.append(len(getattr(server, "servers", [])))
        yield

    config = uvicorn.Config(FastAPI(lifespan=lifespan), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    try:
        assert server.started
        assert listening_during_startup == [0]
        sockets = [sock for listener in server.servers for sock in listener.sockets]
        assert sockets and all(sock.family == socket.AF_INET for sock in sockets)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
