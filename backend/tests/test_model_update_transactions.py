"""Model edits own their write without committing another caller's work."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import httpx
import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI

from infra import database
from llm import catalog


@pytest.fixture
def model_db(isolated_db):
    db = database.get_db()
    db.execute(
        "INSERT INTO models "
        "(id, harness, provider, provider_model_id, litellm_model, display_name, "
        "backend, source, enabled, available, created_at, updated_at) "
        "VALUES ('edit-model', 'native', 'openai', 'synthetic', 'openai/synthetic', "
        "'Original', 'api', 'test', 1, 1, 't', 't')"
    )
    db.commit()
    return db


def _stored(path):
    with closing(sqlite3.connect(path, timeout=0.05)) as observer:
        observer.row_factory = sqlite3.Row
        row = observer.execute("SELECT * FROM models WHERE id='edit-model'").fetchone()
        return dict(row)


def test_model_edit_rejects_foreign_work(isolated_db, model_db):
    before = _stored(isolated_db)
    model_db.execute("INSERT INTO settings VALUES ('foreign', '{}', 't')")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            catalog.update_model("edit-model", {"enabled": False})
        assert model_db.in_transaction
        assert model_db.execute("SELECT value FROM settings WHERE key='foreign'").fetchone()[0] == "{}"
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert observer.execute("SELECT value FROM settings WHERE key='foreign'").fetchone() is None
        assert _stored(isolated_db) == before
    finally:
        model_db.rollback()


def test_rejected_model_edit_leaves_no_partial_change(isolated_db, model_db):
    before = _stored(isolated_db)
    model_db.execute(
        "CREATE TRIGGER reject_edit AFTER UPDATE ON models "
        "WHEN NEW.id='edit-model' BEGIN SELECT RAISE(FAIL, 'reject model edit'); END"
    )
    model_db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="reject model edit"):
        catalog.update_model("edit-model", {"enabled": False, "alias": "Rejected"})
    assert not model_db.in_transaction
    model_db.commit()
    assert _stored(isolated_db) == before
    with closing(sqlite3.connect(isolated_db, timeout=0.05)) as observer:
        observer.execute("BEGIN IMMEDIATE")
        observer.rollback()


@pytest.mark.parametrize("field,value", [
    ("enabled", 0), ("available", 0), ("alias", "Saved alias"),
    ("context_window", 64000), ("display_name", "Edited"), ("description", "Details"),
])
def test_model_edit_preserves_allowed_fields(isolated_db, model_db, field, value):
    assert catalog.update_model("edit-model", {field: value, "provider": "rejected"}) is True
    row = _stored(isolated_db)
    assert row[field] == value
    assert row["provider"] == "openai"
    assert row["updated_at"] != "t"
    assert not model_db.in_transaction


@pytest.mark.parametrize("updates", [{}, {"provider": "rejected"}])
def test_empty_model_edit_does_not_touch_foreign_transaction(isolated_db, model_db, updates):
    before = _stored(isolated_db)
    model_db.execute("INSERT INTO settings VALUES ('foreign', '{}', 't')")
    try:
        assert catalog.update_model("edit-model", updates) is False
        assert model_db.in_transaction
        assert model_db.execute("SELECT value FROM settings WHERE key='foreign'").fetchone()[0] == "{}"
        assert _stored(isolated_db) == before
    finally:
        model_db.rollback()


def test_missing_model_edit_reports_false(isolated_db, model_db):
    before = _stored(isolated_db)
    assert catalog.update_model("missing", {"enabled": False}) is False
    assert _stored(isolated_db) == before
    assert not model_db.in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["enabled", "alias", "commit_failure", "unauthorized", "missing", "invalid"])
async def test_model_patch_http_preserves_durable_result(isolated_db, model_db, monkeypatch, case):
    from api import discovered_models
    from infra import auth

    seed_account("operator", "edit-token")
    before = _stored(isolated_db)
    notifications = []

    def notify():
        assert not model_db.in_transaction
        notifications.append(_stored(isolated_db)["enabled"])
        with closing(sqlite3.connect(isolated_db, timeout=0.05)) as observer:
            observer.execute("BEGIN IMMEDIATE")
            observer.rollback()

    monkeypatch.setattr(discovered_models, "notify_models_changed", notify)
    if case == "commit_failure":
        model_db.set_authorizer(lambda action, arg, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
            else sqlite3.SQLITE_OK)
    app = FastAPI()
    app.include_router(discovered_models.router)
    body = {"alias": "Saved alias"} if case == "alias" else {"enabled": False}
    if case == "invalid":
        body = {"provider": "rejected"}
    token = "invalid-token" if case == "unauthorized" else "edit-token"
    model_id = "missing" if case == "missing" else "edit-model"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
        ) as client:
            response = await client.patch(f"/api/settings/models/{model_id}?token={token}", json=body)
    finally:
        model_db.set_authorizer(None)
    assert response.status_code == {
        "enabled": 200, "alias": 200, "commit_failure": 500,
        "unauthorized": 401, "missing": 404, "invalid": 400,
    }[case]
    if case in ("enabled", "alias"):
        assert response.json() == {"ok": True, "model_id": "edit-model", "updated": list(body)}
        assert _stored(isolated_db)["enabled" if case == "enabled" else "alias"] == (
            0 if case == "enabled" else "Saved alias"
        )
    else:
        assert _stored(isolated_db) == before
    # A hide and a new label both change the picker; a rejected edit changes nothing.
    assert notifications == {"enabled": [0], "alias": [before["enabled"]]}.get(case, [])
    assert not model_db.in_transaction


@pytest.fixture
def harness_row(model_db):
    model_db.execute(
        "INSERT INTO models "
        "(id, harness, provider, provider_model_id, litellm_model, display_name, "
        "backend, source, enabled, available, created_at, updated_at) "
        "VALUES ('codex/gpt-5.6-sol', 'codex', 'codex', 'gpt-5.6-sol', 'codex/gpt-5.6-sol', "
        "'GPT-5.6-Sol', 'cli', 'harness_discovery', 1, 1, 't', 't')"
    )
    model_db.commit()
    return model_db


async def _patch(model_id: str, body: dict):
    from api import discovered_models

    seed_account("operator", "edit-token")
    app = FastAPI()
    app.include_router(discovered_models.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.patch(f"/api/settings/models/{model_id}?token=edit-token", json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["codex/gpt-5.6-sol", "codex%2Fgpt-5.6-sol"])
async def test_harness_row_id_with_slash_is_patchable(isolated_db, harness_row, monkeypatch, model_id):
    from api import discovered_models

    monkeypatch.setattr(discovered_models, "notify_models_changed", lambda: None)
    response = await _patch(model_id, {"enabled": False})
    assert response.status_code == 200
    stored = harness_row.execute("SELECT enabled FROM models WHERE id='codex/gpt-5.6-sol'").fetchone()
    assert stored[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "stored"), [("  Sol  ", "Sol"), ("", None), (None, None)])
async def test_alias_is_trimmed_and_empty_clears(isolated_db, model_db, monkeypatch, sent, stored):
    from api import discovered_models

    monkeypatch.setattr(discovered_models, "notify_models_changed", lambda: None)
    model_db.execute("UPDATE models SET alias='Old' WHERE id='edit-model'")
    model_db.commit()
    response = await _patch("edit-model", {"alias": sent})
    assert response.status_code == 200
    assert _stored(isolated_db)["alias"] == stored


@pytest.mark.asyncio
async def test_non_text_alias_is_rejected(isolated_db, model_db):
    response = await _patch("edit-model", {"alias": 5})
    assert response.status_code == 400
    assert _stored(isolated_db)["alias"] is None
