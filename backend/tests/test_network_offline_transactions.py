"""Offline health results publish only after their coupled records commit."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest


def _state(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT s.status,m.available FROM network_servers s,models m "
            "WHERE s.id='offline-server' AND m.id='offline-model'"
        ).fetchone()


class _OfflineClient:
    async def get(self, *_args, **_kwargs):
        return SimpleNamespace(status_code=503)


@pytest.fixture(params=["managed", "external"])
def offline_server(isolated_db, monkeypatch, request):
    from llm import network_discovery as nd

    provider = "omlx" if request.param == "managed" else "external"
    server = {"id": "offline-server", "kind": request.param, "provider": provider,
              "port": 5200, "status": "online"}
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO network_servers "
            "(id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at) "
            "VALUES ('offline-server',?,?,'instance','Test',5200,'online','t0','t0','t0')",
            (provider, request.param),
        )
        db.execute(
            "INSERT INTO network_server_endpoints "
            "(id,server_id,host,kind,created_at,updated_at) "
            "VALUES ('offline-endpoint','offline-server','127.0.0.1','local','t0','t0')"
        )
        db.execute(
            "INSERT INTO models "
            "(id,provider,provider_model_id,litellm_model,display_name,created_at,updated_at) "
            "VALUES ('offline-model',?,'test-model',?,'Test','t0','t0')",
            (provider, f"{provider}@offline-server/test-model"),
        )
        db.commit()
    loaded = {"offline-server": {"test-model"}}
    monkeypatch.setattr(nd, "_LOADED_BY_SERVER", loaded)
    notifications = []

    def broadcast(message):
        notifications.append((message, _state(isolated_db), dict(loaded)))

    monkeypatch.setattr("infra.state.ws.broadcast_all", broadcast)
    return SimpleNamespace(path=isolated_db, server=server, loaded=loaded,
                           notifications=notifications)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["update", "commit"])
async def test_failed_offline_write_preserves_records_cache_and_notification(
    offline_server, monkeypatch, failure,
):
    from infra import database
    from llm import network_discovery as nd

    if failure == "update":
        with closing(sqlite3.connect(str(offline_server.path))) as db:
            db.execute(
                "CREATE TRIGGER reject_offline BEFORE UPDATE OF available ON models "
                "BEGIN SELECT RAISE(FAIL, 'offline update failed'); END"
            )
            db.commit()
    else:
        original_get_db = database.get_db

        class ModelCommitFailure(database._NoOpClose):
            def __init__(self, connection):
                super().__init__(connection._conn)
                object.__setattr__(self, "model_write", False)

            def execute(self, sql, *args, **kwargs):
                if sql.startswith("UPDATE models SET available"):
                    object.__setattr__(self, "model_write", True)
                return self._conn.execute(sql, *args, **kwargs)

            def commit(self):
                if self.model_write:
                    raise sqlite3.OperationalError("offline commit failed")
                self._conn.commit()

        monkeypatch.setattr(database, "get_db", lambda: ModelCommitFailure(original_get_db()))

    with pytest.raises(sqlite3.Error, match=f"offline {failure} failed"):
        await nd._sweep_one(_OfflineClient(), offline_server.server)
    assert _state(offline_server.path) == ("online", 1)
    assert offline_server.loaded == {"offline-server": {"test-model"}}
    assert offline_server.notifications == []
    with closing(sqlite3.connect(str(offline_server.path), timeout=0.05)) as peer:
        peer.execute("BEGIN IMMEDIATE")
        peer.rollback()


@pytest.mark.asyncio
async def test_offline_notification_observes_committed_records_and_cleared_cache(offline_server):
    from llm import network_discovery as nd

    await nd._sweep_one(_OfflineClient(), offline_server.server)
    assert _state(offline_server.path) == ("offline", 0)
    assert offline_server.notifications == [({"type": "models_updated"}, ("offline", 0), {})]


@pytest.mark.asyncio
async def test_already_offline_sweep_does_not_rewrite_models_or_notify(offline_server):
    from llm import network_discovery as nd

    offline_server.server["status"] = "offline"
    with closing(sqlite3.connect(str(offline_server.path))) as db:
        db.execute("UPDATE network_servers SET status='offline' WHERE id='offline-server'")
        db.commit()
    await nd._sweep_one(_OfflineClient(), offline_server.server)
    assert _state(offline_server.path) == ("offline", 1)
    assert offline_server.loaded == {}
    assert offline_server.notifications == []
