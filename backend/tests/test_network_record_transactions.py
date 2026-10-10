"""Network records commit as declared units, never as partial registration."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest


def _read(path, sql):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(sql).fetchall()


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.05)) as peer:
        peer.execute("BEGIN IMMEDIATE")
        peer.rollback()


@pytest.fixture
def server(isolated_db):
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO network_servers "
            "(id,provider,instance_id,instance_name,port,last_seen_at,created_at,updated_at) "
            "VALUES ('server','omlx','instance','Original',5200,'t0','t0','t0')"
        )
        db.commit()
    return isolated_db


def _change(operation):
    from llm import network_discovery as nd, network_endpoints as ne

    if operation == "server":
        nd.upsert_server("omlx", "instance", "Changed", "127.0.0.1", 5200, "online")
    elif operation == "metadata":
        nd.set_server_metadata("server", api_key="test-key")
    elif operation == "status":
        nd.update_server_status("server", status="online")
    elif operation == "remove":
        nd.remove_server("server")
    elif operation == "unavailable":
        nd._mark_server_models_unavailable("server", "omlx")
    elif operation == "endpoint":
        ne.upsert_endpoint("server", "127.0.0.1", ok=True)
    else:
        raise AssertionError(f"Unknown operation: {operation}")


@pytest.mark.parametrize("operation", ["server", "metadata", "status", "remove", "unavailable", "endpoint"])
def test_standalone_record_write_preserves_unknown_owner(server, operation):
    from infra.database import TransactionOwnershipError, get_db

    db = get_db()
    db.execute("UPDATE network_servers SET instance_name='owner-work' WHERE id='server'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _change(operation)
        assert db.in_transaction
        assert _read(server, "SELECT instance_name FROM network_servers") == [("Original",)]
        db.commit()
        assert _read(server, "SELECT instance_name FROM network_servers") == [("owner-work",)]
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("operation,prefix", [
    ("server", "SELECT id FROM network_servers"),
    ("metadata", "SELECT metadata_json FROM network_servers"),
    ("endpoint", "SELECT id FROM network_server_endpoints"),
])
def test_record_read_holds_its_write_owner(server, monkeypatch, operation, prefix):
    from infra import database
    from llm import network_discovery as nd, network_endpoints as ne

    db = database.get_db()
    observed = []

    class ReadProbe(database._NoOpClose):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith(prefix):
                with closing(sqlite3.connect(str(server), timeout=0.01)) as peer:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        peer.execute("BEGIN IMMEDIATE")
                observed.append(True)
            return self._conn.execute(sql, *args, **kwargs)

    probe = ReadProbe(db._conn)
    for module in (database, nd, ne):
        monkeypatch.setattr(module, "get_db", lambda: probe)
    _change(operation)
    assert observed == [True]
    _writer_available(server)


@pytest.mark.parametrize("existing", [False, True])
def test_endpoint_failure_rolls_back_its_server(server, existing):
    from llm.network_discovery import upsert_server

    with closing(sqlite3.connect(str(server))) as db:
        db.execute(
            "CREATE TRIGGER reject_endpoint BEFORE INSERT ON network_server_endpoints "
            "BEGIN SELECT RAISE(FAIL, 'endpoint unavailable'); END"
        )
        db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="endpoint unavailable"):
        upsert_server(
            "omlx", "instance" if existing else "new", "Changed", "127.0.0.1", 5200, "online",
        )
    assert _read(server, "SELECT instance_name FROM network_servers") == [("Original",)]
    assert _read(server, "SELECT host FROM network_server_endpoints") == []
    _writer_available(server)


def _registration_records(path):
    return _read(path,
        "SELECT s.instance_name,s.status,e.host,json_extract(s.metadata_json,'$.api_key') "
        "FROM network_servers s JOIN network_server_endpoints e ON e.server_id=s.id"
    )


@pytest.fixture
def registration(isolated_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.network_servers import router
    from llm import network_discovery as nd
    from omlx_provider_fixtures import _patch_httpx, _resp, _Router

    external = _Router({
        ("GET", "/health"): _resp(200, {"status": "ok"}),
        ("GET", "/v1/models"): _resp(200, {"data": []}),
    })

    def probe(request):
        _writer_available(isolated_db)
        return external(request)

    _patch_httpx(monkeypatch, probe)
    observed, completed = [], threading.Event()

    async def discover(_server_id):
        try:
            _writer_available(isolated_db)
            observed.extend(_registration_records(isolated_db))
        finally:
            completed.set()

    monkeypatch.setattr(nd, "discover_and_notify", discover)
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "u-test", "name": "u-test"}
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(
            client=client, path=isolated_db, observed=observed,
            completed=completed, external=external,
        )


def test_registration_metadata_failure_leaves_no_partial_records(registration):
    with closing(sqlite3.connect(str(registration.path))) as db:
        db.execute(
            "CREATE TRIGGER reject_metadata BEFORE UPDATE OF metadata_json ON network_servers "
            "BEGIN SELECT RAISE(FAIL, 'metadata unavailable'); END"
        )
        db.commit()
    response = registration.client.post("/api/settings/network-servers", json={
        "name": "Test server", "hostname": "127.0.0.1", "port": 5200, "api_key": "test-key",
    })
    assert response.status_code == 500
    assert _read(registration.path, "SELECT id FROM network_servers") == []
    assert _read(registration.path, "SELECT id FROM network_server_endpoints") == []
    assert not registration.completed.is_set()
    _writer_available(registration.path)


def test_registration_commits_before_response_and_discovery(registration):
    response = registration.client.post("/api/settings/network-servers", json={
        "name": "Test server", "hostname": "127.0.0.1", "port": 5200, "api_key": "test-key",
    })
    assert response.status_code == 200
    expected = [("Test server", "online", "127.0.0.1", "test-key")]
    assert _registration_records(registration.path) == expected
    assert registration.completed.wait(timeout=2)
    assert registration.observed == expected
    server_id = _read(registration.path, "SELECT id FROM network_servers")[0][0]
    assert response.json() == {"ok": True, "server_id": server_id, "status": "online"}
    assert [url for _, url, _ in registration.external.calls] == [
        "http://127.0.0.1:5200/health", "http://127.0.0.1:5200/v1/models",
    ]


def test_failed_probe_keeps_the_last_success_and_latency(server, monkeypatch):
    from llm import network_endpoints as ne

    monkeypatch.setattr(ne, "_now_iso", lambda: "t1")
    ne.upsert_endpoint("server", "127.0.0.1", ok=True, latency_ms=4)
    monkeypatch.setattr(ne, "_now_iso", lambda: "t2")
    ne.upsert_endpoint("server", "127.0.0.1", ok=False, latency_ms=900)
    assert _read(server,
        "SELECT kind,last_ok_at,last_latency_ms,updated_at FROM network_server_endpoints"
    ) == [("local", "t1", 4, "t2")]


def test_readding_server_keeps_identity_and_existing_metadata(server):
    from llm import network_discovery as nd

    nd.set_server_metadata("server", api_key="retained-key", context_window=1234)
    sid = nd.upsert_server(
        "omlx", "instance", "Renamed", "127.0.0.1", 5200, "online", machine_id="machine",
    )
    nd.set_server_metadata(sid, api_key=None, context_window=5678)
    assert sid == "server"
    assert _read(server,
        "SELECT instance_name,machine_id,json_extract(metadata_json,'$.api_key'),"
        "json_extract(metadata_json,'$.context_window') FROM network_servers"
    ) == [("Renamed", "machine", "retained-key", 5678)]


def test_cleared_metadata_keys_are_removed_and_the_rest_kept(server):
    from llm import network_discovery as nd

    nd.set_server_metadata(
        "server", api_key="old-key", cache_warm_seconds=3600, context_window=1234,
    )
    nd.set_server_metadata(
        "server", clear=("cache_warm_seconds", "api_key"), base_url="https://h/v1",
    )
    assert nd.read_server_metadata("server") == {"context_window": 1234, "base_url": "https://h/v1"}


def test_unreadable_metadata_reads_as_none_not_empty(server):
    from llm import network_discovery as nd

    with closing(sqlite3.connect(str(server))) as db:
        db.execute("UPDATE network_servers SET metadata_json='{broken' WHERE id='server'")
        db.commit()
    assert nd.read_server_metadata("server") is None
    assert nd.get_server_metadata("server") == {}
