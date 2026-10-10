"""Public catalog discovery protects one snapshot and foreign transaction work."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest


class _ObservedConnection(sqlite3.Connection):
    closed = False
    reject_commit = False

    def execute(self, statement, *args, **kwargs):
        if statement.lstrip().startswith("SELECT id") and "FROM models" in statement:
            self.read_owners.append(self.in_transaction)
        return super().execute(statement, *args, **kwargs)

    def commit(self):
        if self.reject_commit:
            raise sqlite3.OperationalError("snapshot commit rejected")
        super().commit()

    def close(self):
        self.closed = True
        super().close()


def _rows(path, query):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(query).fetchall()


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.05)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture(params=["managed", "external"])
def snapshot(request, isolated_db, monkeypatch):
    from infra import database
    from llm import catalog_network_models, external_provider

    managed = request.param == "managed"
    module = catalog_network_models if managed else external_provider
    provider = "omlx" if managed else "external"
    server = {"id": "snapshot-server", "provider": provider, "status": "online",
              "instance_name": "Fixture provider", "dialect": "openai"}
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) "
                   "VALUES ('snapshot-owner','test-user','before','t','t')")
        for model_id in ("snapshot-stale", "snapshot-reviewer"):
            db.execute(
                "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
                "display_name,source,available,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'discovered',1,'t','t')",
                (model_id, provider, model_id, f"{provider}@snapshot-server/{model_id}", model_id),
            )
        db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('agents',?,'t')",
                   (json.dumps({"defaults": {"host_access_reviewer_model": "snapshot-reviewer"}}),))
        db.commit()

    conn = sqlite3.connect(str(isolated_db), factory=_ObservedConnection, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.read_owners = []
    # Cover the old module factory and the canonical owner's factory without
    # changing the actual SQLite transaction, statement or worker behavior.
    monkeypatch.setattr(module, "get_db", lambda: conn, raising=False)
    monkeypatch.setattr(database, "get_db", lambda: conn)
    model_ids = ["snapshot-a", "snapshot-b"]
    if managed:
        monkeypatch.setattr(module, "_network_discovery_config",
                            lambda _sid: (server, "http://fixture/v1", "http://fixture/admin"))

        async def fetch(*_args):
            return ([{"id": model_id} for model_id in model_ids], {})

        monkeypatch.setattr(module, "_fetch_omlx_canonical_models", fetch)
        discover = module.discover_network_models
    else:
        caps = {"context_window": None, "supports_vision": 0,
                "supports_tools": 1, "supports_reasoning": 0}
        monkeypatch.setattr(module, "_external_discovery_config",
                            lambda _sid: (server, "http://fixture/v1", None, caps))

        async def fetch(*_args):
            return list(model_ids)

        monkeypatch.setattr(module, "fetch_external_models", fetch)
        discover = module.discover_external_models

    # Rows are matched by transport string: External rows key provider_model_id
    # by endpoint, managed rows do not; the served id is the tail of both.
    unrelated = _rows(isolated_db, "SELECT id,available FROM models "
                      "WHERE litellm_model NOT LIKE '%/snapshot-%' ORDER BY id")
    yield SimpleNamespace(path=isolated_db, db=conn, models=model_ids, discover=discover)
    assert _rows(isolated_db, "SELECT id,available FROM models "
                 "WHERE litellm_model NOT LIKE '%/snapshot-%' ORDER BY id") == unrelated
    if not conn.closed:
        conn.rollback()
        conn.close()


def _assert_original_snapshot(path):
    assert _rows(path, "SELECT id,available FROM models "
                 "WHERE litellm_model LIKE '%/snapshot-%' ORDER BY id") == [
        ("snapshot-reviewer", 1), ("snapshot-stale", 1),
    ]


@pytest.mark.asyncio
async def test_snapshot_refuses_to_commit_unknown_outer_work(snapshot):
    snapshot.db.execute("UPDATE threads SET title='outer' WHERE id='snapshot-owner'")
    result = await snapshot.discover("snapshot-server")
    assert result["ok"] is False
    assert "db=" in result["error"]
    assert snapshot.db.in_transaction and not snapshot.db.closed
    assert _rows(snapshot.path, "SELECT title FROM threads WHERE id='snapshot-owner'") == [("before",)]
    _assert_original_snapshot(snapshot.path)
    snapshot.db.commit()
    assert _rows(snapshot.path, "SELECT title FROM threads WHERE id='snapshot-owner'") == [("outer",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_snapshot_protects_before_reads_and_commits_complete_result(snapshot, empty):
    if empty:
        snapshot.models.clear()
    result = await snapshot.discover("snapshot-server")
    assert result == {"ok": True, "discovered": 0 if empty else 2, "updated": 0, "missing": 2}
    assert snapshot.db.read_owners and all(snapshot.db.read_owners)
    assert snapshot.db.closed
    served = "substr(litellm_model, instr(litellm_model, '/') + 1)"
    assert _rows(snapshot.path, f"SELECT {served},available FROM models "
                 f"WHERE litellm_model LIKE '%/snapshot-%' ORDER BY {served}") == (
        [("snapshot-reviewer", 0)] if empty else
        [("snapshot-a", 1), ("snapshot-b", 1), ("snapshot-reviewer", 0)]
    )
    _writer_available(snapshot.path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["second_insert", "stale", "commit"])
async def test_snapshot_failure_rolls_back_before_reporting_error(snapshot, failure):
    if failure == "commit":
        snapshot.db.reject_commit = True
        error = "snapshot commit rejected"
    else:
        event = ("INSERT ON models WHEN NEW.litellm_model LIKE '%/snapshot-b'"
                 if failure == "second_insert" else
                 "DELETE ON models WHEN OLD.id='snapshot-stale'")
        snapshot.db.execute(f"CREATE TRIGGER reject_snapshot BEFORE {event} "
                            "BEGIN SELECT RAISE(FAIL, 'snapshot statement rejected'); END")
        snapshot.db.commit()
        error = "snapshot statement rejected"

    result = await snapshot.discover("snapshot-server")
    assert result == {"ok": False, "discovered": 1 if failure == "second_insert" else 2,
                      "updated": 0, "missing": 2 if failure == "commit" else 0, "error": error}
    assert snapshot.db.closed
    _assert_original_snapshot(snapshot.path)
    _writer_available(snapshot.path)
