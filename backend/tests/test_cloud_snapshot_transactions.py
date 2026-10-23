"""Cloud discovery persists one snapshot without losing saved reasoning truth."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI

from infra import database
from llm import catalog_cloud
from llm.hosted_catalog import cloud_models, loader


@pytest.fixture(autouse=True)
def reset_snapshot_cache():
    loader._reset_cache_for_tests()
    yield
    loader._reset_cache_for_tests()


def _models(provider="openai"):
    family = "gpt-5" if provider == "openai" else "claude-sonnet-4-6"
    return [{
        "provider_model_id": name, "litellm_model": f"{provider}/{name}",
        "display_name": name + " refreshed", "featured": True, "context_window": 64000,
        "supports_vision": False, "supports_tools": True, "supports_reasoning": True,
        "input_cost_per_1m": 1, "output_cost_per_1m": 2,
    } for name in (f"{family}-probe-first", f"{family}-probe-second")]


def _seed(db, models, shape):
    for model in models:
        db.execute(
            "INSERT INTO models "
            "(id,harness,provider,provider_model_id,litellm_model,display_name,"
            "backend,source,enabled,available,metadata_json,created_at,updated_at) "
            "VALUES (?,'native','openai',?,?,'before','api','test',0,0,?,'t','t')",
            (model["provider_model_id"], model["provider_model_id"], model["litellm_model"],
             json.dumps({"reasoning_shape": shape})),
        )
    db.commit()


def _stored(path):
    with closing(sqlite3.connect(path, timeout=0.05)) as observer:
        observer.row_factory = sqlite3.Row
        rows = observer.execute("SELECT * FROM models WHERE provider_model_id LIKE '%-probe-%'")
        return {r["provider_model_id"]: dict(r) for r in rows}


@pytest.mark.parametrize("existing", [False, True])
def test_every_cloud_batch_row_is_durable(isolated_db, existing):
    models = _models()
    db = database.get_db()
    if existing:
        _seed(db, models, {"type": "boolean", "default": True})
    reads_owned = []

    def trace(sql):
        if sql.startswith("SELECT id, enabled"):
            reads_owned.append(db.in_transaction)

    db.set_trace_callback(trace)
    try:
        result = catalog_cloud._persist_cloud_models("openai", models, "now")
    finally:
        db.set_trace_callback(None)
    assert reads_owned and all(reads_owned)
    rows = _stored(isolated_db)
    assert len(rows) == len(result) == 2
    for model in models:
        row = rows[model["provider_model_id"]]
        assert row["display_name"] == model["display_name"]
        assert row["enabled"] == (0 if existing else 1)
        assert row["available"] == 1
        assert row["context_window"] == 64000
        assert row["default_max_tokens"] is None
        assert row["default_temperature"] is None
        assert json.loads(row["metadata_json"])["input_cost_per_1m"] == 1


def test_cloud_batch_preserves_foreign_work(isolated_db):
    db = database.get_db()
    db.execute("INSERT INTO settings VALUES ('foreign', '{}', 't')")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            catalog_cloud._persist_cloud_models("openai", _models(), "now")
        assert db.in_transaction
        assert db.execute("SELECT value FROM settings WHERE key='foreign'").fetchone()[0] == "{}"
        assert _stored(isolated_db) == {}
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert observer.execute("SELECT value FROM settings WHERE key='foreign'").fetchone() is None
    finally:
        db.rollback()


def test_late_cloud_statement_rolls_back_whole_batch(isolated_db):
    db = database.get_db()
    models = _models()
    _seed(db, models, {})
    before = _stored(isolated_db)
    db.execute(
        "CREATE TRIGGER reject_cloud AFTER UPDATE ON models "
        "WHEN NEW.provider_model_id='gpt-5-probe-second' "
        "BEGIN SELECT RAISE(FAIL, 'reject cloud row'); END"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="reject cloud row"):
        catalog_cloud._persist_cloud_models("openai", models, "now")
    assert not db.in_transaction
    db.commit()
    assert _stored(isolated_db) == before
    with closing(sqlite3.connect(isolated_db, timeout=0.05)) as observer:
        observer.execute("BEGIN IMMEDIATE")
        observer.rollback()


def test_cold_snapshot_files_are_read_before_writer(isolated_db, monkeypatch):
    from pathlib import Path

    db = database.get_db()
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.parent.name == "cloud-models":
            reads.append(path.name)
            assert not db.in_transaction
            with closing(sqlite3.connect(isolated_db, timeout=0.05)) as observer:
                observer.execute("BEGIN IMMEDIATE")
                observer.rollback()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    catalog_cloud._persist_cloud_models("openai", _models(), "now")
    assert sorted(reads) == ["anthropic.json", "openai.json"]


@pytest.mark.parametrize("needs_fallback", [False, True])
def test_broken_fallback_only_fails_when_needed(isolated_db, monkeypatch, tmp_path, needs_fallback):
    db = database.get_db()
    models = _models()
    _seed(db, models, {})
    if needs_fallback:
        db.execute("UPDATE models SET metadata_json='{}' WHERE provider_model_id='gpt-5-probe-second'")
        db.commit()
    before = _stored(isolated_db)
    monkeypatch.setattr(loader, "_SNAPSHOT_DIR", tmp_path / "missing-snapshot")
    if needs_fallback:
        with pytest.raises(loader.CatalogSnapshotError, match="missing or corrupt"):
            catalog_cloud._persist_cloud_models("openai", models, "now")
        assert _stored(isolated_db) == before
    else:
        catalog_cloud._persist_cloud_models("openai", models, "now")
        for row in _stored(isolated_db).values():
            assert row["display_name"].endswith(" refreshed")
            assert json.loads(row["metadata_json"])["reasoning_shape"] == {}
    assert not db.in_transaction


def test_saved_shape_changed_during_preparation_wins(isolated_db, monkeypatch):
    models = _models()[:1]
    _seed(database.get_db(), models, None)
    original = cloud_models.reasoning_shape_for
    shape = {"type": "effort_levels", "values": ["custom"], "default": "custom"}
    prepared = []

    def prepare(provider, model_id):
        fallback = original(provider, model_id)
        with closing(sqlite3.connect(isolated_db, timeout=0.05)) as other:
            other.execute("UPDATE models SET metadata_json=? WHERE provider_model_id=?",
                          (json.dumps({"reasoning_shape": shape}), model_id))
            other.commit()
        prepared.append(model_id)
        return fallback

    monkeypatch.setattr(cloud_models, "reasoning_shape_for", prepare)
    catalog_cloud._persist_cloud_models("openai", models, "now")
    assert prepared == ["gpt-5-probe-first"]
    assert json.loads(_stored(isolated_db)["gpt-5-probe-first"]["metadata_json"])["reasoning_shape"] == shape


def test_corrupt_stored_shape_is_not_hidden_by_fallback(isolated_db):
    db = database.get_db()
    models = _models()
    _seed(db, models, None)
    db.execute("UPDATE models SET metadata_json='{broken' WHERE provider_model_id='gpt-5-probe-second'")
    db.commit()
    before = _stored(isolated_db)
    with pytest.raises(json.JSONDecodeError):
        catalog_cloud._persist_cloud_models("openai", models, "now")
    assert _stored(isolated_db) == before
    assert not db.in_transaction


def test_empty_cloud_batch_does_not_need_snapshot(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_SNAPSHOT_DIR", tmp_path / "missing-snapshot")
    assert catalog_cloud._persist_cloud_models("openai", [], "now") == []
    assert _stored(isolated_db) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_commit", [False, True])
async def test_cloud_discovery_http_reports_only_committed_rows(
    isolated_db, monkeypatch, caplog, fail_commit, tmp_path,
):
    from api import discovered_models, providers
    from infra import auth, key_store_providers

    seed_account("operator", "cloud-token")
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    key_store_providers.set_provider_key("anthropic", "snapshot-test-value-1")
    models = _models("anthropic")
    monkeypatch.setattr(catalog_cloud, "_anthropic_discovered", lambda: models)
    db = database.get_db()
    if fail_commit:
        db.set_authorizer(lambda action, arg, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
            else sqlite3.SQLITE_OK)
    app = FastAPI()
    app.include_router(providers.router)
    app.include_router(discovered_models.router)
    try:
        with caplog.at_level("INFO", logger=catalog_cloud.__name__):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
            ) as client:
                response = await client.get("/api/settings/providers/anthropic/models?token=cloud-token")
                inventory = await client.get("/api/settings/models?token=cloud-token")
    finally:
        db.set_authorizer(None)
    assert response.status_code == (500 if fail_commit else 200)
    assert inventory.status_code == 200
    saved = [m for m in inventory.json()["models"] if "-probe-" in m["provider_model_id"]]
    assert len(saved) == len(_stored(isolated_db)) == (0 if fail_commit else 2)
    assert not db.in_transaction
    successes = [r for r in caplog.records if r.name == catalog_cloud.__name__ and r.levelname == "INFO"]
    assert len(successes) == (0 if fail_commit else 1)
    if not fail_commit:
        result = response.json()
        assert result["ok"] is True
        assert [m["id"] for m in result["models"]] == [m["provider_model_id"] for m in models]


@pytest.mark.asyncio
async def test_cloud_discovery_broadcasts_only_when_the_stored_models_changed(isolated_db, monkeypatch):
    from unittest.mock import Mock
    from infra.state import ws

    models = _models("anthropic")
    monkeypatch.setattr(catalog_cloud, "_anthropic_discovered", lambda: models)
    notify = Mock()
    monkeypatch.setattr(ws, "notify_models_changed", notify)

    await catalog_cloud.discover_cloud_models("anthropic", "k")
    assert notify.call_count == 1  # first insert is a change

    await catalog_cloud.discover_cloud_models("anthropic", "k")
    assert notify.call_count == 1  # same models, newer timestamps: no broadcast

    models[0] = {**models[0], "display_name": "Renamed"}
    await catalog_cloud.discover_cloud_models("anthropic", "k")
    assert notify.call_count == 2
