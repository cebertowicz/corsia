from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth_fixtures import create_accounts_tables, seed_test_accounts  # noqa: E402
from infra.key_store_schema import KEY_STORE_SCHEMA_SQL  # noqa: E402


def _resp(status: int, body: Any = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, request=httpx.Request("GET", "http://x/"))
    return httpx.Response(status, json=body, request=httpx.Request("GET", "http://x/"))


def _login_ok() -> httpx.Response:
    """oMLX's admin sign-in: 200 and the session cookie its admin routes require."""
    return httpx.Response(
        200, json={"success": True}, headers={"set-cookie": "omlx_admin_session=fixture; Path=/"},
        request=httpx.Request("POST", "http://x/"),
    )


class _Router:

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]):
        self.routes = {**routes}
        self.routes.setdefault(("POST", "/admin/api/login"), _login_ok())
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method
        self.calls.append((method, url))
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp
        return _resp(404)


_ACTIVE_ROUTER: dict[str, _Router | None] = {"router": None}


def _reset_database_pool(database, *, reconfigure=None) -> None:
    database.shutdown_pool(reconfigure)


def _patch_httpx_once(monkeypatch):
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        router = _ACTIVE_ROUTER["router"]
        if router is not None:
            kwargs["transport"] = httpx.MockTransport(router)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _set_router(router: _Router):
    _ACTIVE_ROUTER["router"] = router


def _patch_httpx(monkeypatch, router: _Router):
    _patch_httpx_once(monkeypatch)
    _set_router(router)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    from infra import config as _cfg
    from infra import database as _db

    def reconfigure() -> None:
        monkeypatch.setattr(_cfg, "DB_PATH", db_file)
        monkeypatch.setattr(_db, "DB_PATH", db_file)

    _reset_database_pool(_db, reconfigure=reconfigure)

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE network_servers (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'omlx',
            kind TEXT NOT NULL DEFAULT 'managed',
            dialect TEXT,
            instance_id TEXT NOT NULL,
            machine_id TEXT,
            instance_name TEXT NOT NULL,
            port INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'offline',
            source TEXT DEFAULT 'manual',
            latency_ms INTEGER,
            memory_total_mb INTEGER,
            memory_used_mb INTEGER,
            gpu_info TEXT,
            metadata_json TEXT,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, instance_id)
        );
        CREATE TABLE network_server_endpoints (
            id TEXT PRIMARY KEY,
            server_id TEXT NOT NULL REFERENCES network_servers(id) ON DELETE CASCADE,
            host TEXT NOT NULL,
            kind TEXT NOT NULL,
            last_ok_at TEXT,
            last_latency_ms INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(server_id, host)
        );
        """
    )
    conn.execute(
        """CREATE TABLE models (
            id TEXT PRIMARY KEY,
            harness TEXT NOT NULL DEFAULT 'native',
            provider TEXT NOT NULL,
            provider_model_id TEXT NOT NULL,
            litellm_model TEXT NOT NULL,
            display_name TEXT NOT NULL,
            alias TEXT,
            description TEXT,
            backend TEXT NOT NULL DEFAULT 'api',
            source TEXT NOT NULL DEFAULT 'seed',
            enabled INTEGER NOT NULL DEFAULT 1,
            available INTEGER NOT NULL DEFAULT 1,
            context_window INTEGER DEFAULT 128000,
            supports_vision INTEGER DEFAULT 0,
            supports_tools INTEGER DEFAULT 1,
            supports_reasoning INTEGER DEFAULT 0,
            default_temperature REAL DEFAULT 0.3,
            default_max_tokens INTEGER DEFAULT 8192,
            metadata_json TEXT,
            last_used_at TEXT,
            runtime_state TEXT DEFAULT 'idle',
            last_error TEXT,
            load_started_at TEXT,
            load_completed_at TEXT,
            inference_overrides TEXT,
            load_overrides TEXT,
            estimated_memory_gb REAL,
            discovered_at TEXT,
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, provider_model_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE connections (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            provider TEXT NOT NULL UNIQUE,
            config TEXT,
            status TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            scope TEXT NOT NULL DEFAULT 'global',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    # This database is built table by table rather than from the whole schema,
    # but a route still resolves its caller against the account tables, and the
    # server key through its key store row, so they are not optional here.
    create_accounts_tables(conn)
    conn.executescript(KEY_STORE_SCHEMA_SQL)
    conn.commit()
    conn.close()
    seed_test_accounts()
    from infra.database import get_db
    from infra import key_store
    from llm import network_endpoints as ne

    db = get_db()
    now = "2026-10-07T00:00:00+00:00"
    db.execute(
        """INSERT INTO network_servers
           (id, provider, kind, instance_id, instance_name, port, status,
            source, metadata_json, last_seen_at, created_at, updated_at)
           VALUES ('srv-test', 'omlx', 'managed', 'inst-1', 'Mac', 5100, 'online',
                   'manual', '{}', ?, ?, ?)""",
        (now, now, now),
    )
    db.commit()
    ne.upsert_endpoint("srv-test", "127.0.0.1", ok=True)
    key_store.set_managed_value("server-srv-test", "catalog-fixture-key")

    yield db_file

    _reset_database_pool(_db)


def _stub_network_helpers(monkeypatch, server_id: str = "srv-test"):
    from llm import network_discovery as nd
    from llm import network_endpoints as ne

    monkeypatch.setattr(
        nd,
        "get_server",
        lambda sid: {
            "id": sid,
            "provider": "omlx",
            "instance_id": "inst-1",
            "instance_name": "Mac",
            "host": "127.0.0.1",
            "port": 5100,
            "status": "online",
        }
        if sid == server_id
        else None,
    )
    monkeypatch.setattr(ne, "build_api_base", lambda sid: "http://127.0.0.1:5100/v1")
    monkeypatch.setattr(
        ne, "build_mgmt_base", lambda sid: "http://127.0.0.1:5100/admin/api"
    )
    from llm import reasoning_config as rc

    monkeypatch.setattr(rc, "reasoning_config_for", lambda *a, **kw: None)
