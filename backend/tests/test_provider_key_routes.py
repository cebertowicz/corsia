"""Provider keys are written through the key store, and only by a signed-in person.

PUT/DELETE /api/settings/providers/{name}: a session token sets and clears a
key; an api or service token is refused when the request sets or clears one,
and still changes a setting that carries no key. The key never reaches the
settings table or the process environment. Every value here is synthetic.
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import seed_account
from api.key_session_guard import PERSON_ONLY
from infra import key_store, key_store_providers
from infra.database import get_db
from infra.settings import get_setting
from key_store_fixtures import fail_the_commit_after_a_key_write, values_after_a_sweep

VALUE = "provider-route-value-0123456789"
SESSION = "provider-session-token-01"
API = "provider-api-token-01"
SERVICE = "provider-service-token-01"


@pytest.fixture
def client(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("api.providers.notify_models_changed", lambda: None)
    seed_account("person", SESSION, kind="session")
    seed_account("agent", API, kind="api")
    seed_account("svc", SERVICE, kind="service")
    from api.providers import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_a_session_sets_lists_and_clears_a_key_without_touching_settings_or_environment(client, caplog):
    caplog.set_level(logging.INFO, logger="corsia.key_audit")
    put = client.put("/api/settings/providers/anthropic", json={"api_key": VALUE}, headers=_header(SESSION))
    assert put.status_code == 200
    assert put.json()["has_key"] is True
    assert VALUE not in put.text
    assert key_store.resolve_provider("anthropic") == VALUE
    assert "apiKey" not in (get_setting("providers") or {}).get("anthropic", {})
    assert "ANTHROPIC_API_KEY" not in os.environ

    listed = client.get("/api/settings/providers", headers=_header(API)).json()["providers"]
    anthropic = next(p for p in listed if p["name"] == "anthropic")
    assert anthropic["has_key"] is True
    assert anthropic["masked_key"] == "••••" + VALUE[-4:]
    assert VALUE not in str(listed)

    deleted = client.delete("/api/settings/providers/anthropic", headers=_header(SESSION))
    assert deleted.status_code == 200
    assert key_store.resolve_provider("anthropic") is None
    assert key_store.resolve_names(["provider-anthropic"]) == {}
    row = get_db().execute("SELECT 1 FROM key_store_entries WHERE name = 'provider-anthropic'").fetchone()
    assert row is None

    audit = [record.getMessage() for record in caplog.records if record.name == "corsia.key_audit"]
    assert audit == [
        "key write: kind=provider name=anthropic token_kind=session",
        "key clear: kind=provider name=anthropic token_kind=session",
    ]
    assert all(VALUE not in line for line in audit)


def test_the_query_parameter_session_the_frontend_uses_is_a_session(client):
    response = client.put(f"/api/settings/providers/openai?token={SESSION}", json={"api_key": VALUE})
    assert response.status_code == 200
    assert key_store.resolve_provider("openai") == VALUE


@pytest.mark.parametrize("token", [API, SERVICE])
def test_other_tokens_cannot_set_or_clear_a_key(client, token, caplog):
    caplog.set_level(logging.WARNING, logger="corsia.key_audit")
    client.put("/api/settings/providers/openai", json={"api_key": VALUE}, headers=_header(SESSION))

    refused_set = client.put("/api/settings/providers/openai", json={"api_key": "other-value-9999"}, headers=_header(token))
    refused_clear = client.delete("/api/settings/providers/openai", headers=_header(token))
    for response in (refused_set, refused_clear):
        assert response.status_code == 403
        assert response.json()["detail"] == PERSON_ONLY
        assert VALUE not in response.text and "other-value-9999" not in response.text
    assert key_store.resolve_provider("openai") == VALUE
    refused = [record.getMessage() for record in caplog.records if record.name == "corsia.key_audit"]
    assert len(refused) == 2 and all("refused" in line and "9999" not in line for line in refused)


def test_a_setting_that_carries_no_key_still_works_for_an_api_token(client):
    client.put("/api/settings/providers/openai", json={"api_key": VALUE}, headers=_header(SESSION))
    response = client.put("/api/settings/providers/openai", json={"enabled": False}, headers=_header(API))
    assert response.status_code == 200
    assert (get_setting("providers") or {})["openai"] == {"enabled": False}
    assert key_store.resolve_provider("openai") == VALUE


def test_enabling_a_provider_with_no_key_is_refused_and_a_refused_key_stores_nothing(client):
    assert client.put("/api/settings/providers/openai", json={"enabled": True}, headers=_header(API)).status_code == 400
    bad = client.put("/api/settings/providers/openai", json={"api_key": "has a space"}, headers=_header(SESSION))
    assert bad.status_code == 400
    assert "has a space" not in bad.text
    assert key_store.resolve_provider("openai") is None


def test_the_models_and_test_routes_read_the_store(client, monkeypatch):
    seen: list[str] = []

    async def discover(name, key):
        seen.append(key)
        return {"ok": True, "models": []}

    monkeypatch.setattr("llm.catalog.discover_cloud_models", discover)
    empty = client.get("/api/settings/providers/openai/models", headers=_header(API)).json()
    assert empty["error"] == "No API key configured"
    client.put("/api/settings/providers/openai", json={"api_key": VALUE}, headers=_header(SESSION))
    assert client.get("/api/settings/providers/openai/models", headers=_header(API)).json()["ok"] is True
    assert seen == [VALUE]


def test_a_hidden_provider_keeps_its_key_listed_until_it_is_removed(client):
    key_store_providers.set_provider_key("groq", VALUE)
    listed = client.get("/api/settings/providers", headers=_header(API)).json()["providers"]
    assert next(p for p in listed if p["name"] == "groq")["unsupported"] is True
    assert client.delete("/api/settings/providers/groq", headers=_header(SESSION)).status_code == 200
    listed = client.get("/api/settings/providers", headers=_header(API)).json()["providers"]
    assert "groq" not in {p["name"] for p in listed}
    assert key_store.resolve_provider("groq") is None


def test_the_webhook_secret_is_read_from_the_store(isolated_db, tmp_path, monkeypatch):
    from api.webhook_receivers import receiver_secret
    from infra.settings import set_setting

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    set_setting("webhook", {"github_webhook_secret": "settings-hook-value"})
    assert receiver_secret("github") is None
    key_store.set_value("github-webhook", "stored-hook-value-1")
    assert receiver_secret("github") == "stored-hook-value-1"


def test_the_store_refuses_names_code_owns_and_provider_entries_are_not_listed(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    for name in ("provider-anthropic", "provider-x", "server-ns-0123456789", "mcp-docs-token"):
        with pytest.raises(key_store.KeyStoreError):
            key_store.add_entry(name, "Label", "", added_by_thread_id=None, added_by_name=None)
    key_store_providers.set_provider_key("anthropic", VALUE)
    assert [entry["name"] for entry in key_store.list_entries()] == ["web-search", "github-webhook"]
    with pytest.raises(key_store.KeyStoreError):
        key_store.set_value("provider-anthropic", VALUE)
    with pytest.raises(key_store.KeyStoreError):
        key_store.set_managed_value("provider-nonsense", VALUE)


def test_the_provider_list_notes_an_ignored_environment_key_by_presence(client, monkeypatch):
    from infra import ignored_environment

    monkeypatch.setenv("OPENAI_API_KEY", "environment-value-not-to-be-read")
    ignored_environment.note_ignored_environment()
    try:
        listed = client.get("/api/settings/providers", headers=_header(API)).json()["providers"]
    finally:
        monkeypatch.setattr(ignored_environment, "_present", frozenset())
    assert {p["name"]: p["ignored_env_var"] for p in listed if p["type"] == "cloud"} == {
        "anthropic": None, "openai": "OPENAI_API_KEY",
    }
    assert "environment-value" not in str(listed)
    assert key_store.resolve_provider("openai") is None


def test_the_connection_check_still_reports_no_key_a_refused_key_and_a_good_key(client, monkeypatch):
    import httpx

    status = {"code": 401}
    sent: list[str] = []

    class _Response:
        status_code = 0

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            sent.append(headers["Authorization"])
            response = _Response()
            response.status_code = status["code"]
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    path = "/api/settings/providers/openai/test"
    assert client.post(path, headers=_header(API)).json() == {"ok": False, "error": "No API key configured"}
    client.put("/api/settings/providers/openai", json={"api_key": VALUE}, headers=_header(SESSION))
    assert client.post(path, headers=_header(API)).json() == {"ok": False, "error": "Invalid API key"}
    status["code"] = 200
    assert client.post(path, headers=_header(API)).json()["ok"] is True
    assert sent == [f"Bearer {VALUE}", f"Bearer {VALUE}"]


def test_a_saved_key_follows_the_base_url_only_to_the_same_host(client):
    def put(body: dict, token: str = API):
        return client.put("/api/settings/providers/openai", json=body, headers=_header(token))

    def saved_base() -> str | None:
        return (get_setting("providers") or {})["openai"].get("apiBase")

    put({"api_key": VALUE}, SESSION)
    assert put({"api_base": "https://api.openai.com/v1"}).status_code == 400
    assert put({"api_key": VALUE, "api_base": "https://api.openai.com/v1"}, SESSION).status_code == 200
    assert put({"api_base": "https://api.openai.com:443/v2"}).status_code == 200

    moved = put({"api_base": "https://elsewhere.example/v1"})
    assert moved.status_code == 400
    assert moved.json()["detail"].startswith("Enter the API key for the new host.")
    assert saved_base() == "https://api.openai.com:443/v2"
    assert key_store.resolve_provider("openai") == VALUE

    assert put({"api_key": VALUE + "-new-host", "api_base": "https://elsewhere.example/v1"}, SESSION).status_code == 200
    assert saved_base() == "https://elsewhere.example/v1"

    back_to_vendor = put({"api_base": ""})
    assert back_to_vendor.status_code == 400
    assert saved_base() == "https://elsewhere.example/v1"


@pytest.mark.parametrize("failure", ["host", "commit"])
@pytest.mark.parametrize("provider, old_base, new_base", [
    ("openai", "https://api.openai.com/v1", "https://elsewhere.example/v1"),
])
def test_a_failed_base_write_keeps_the_old_key(client, monkeypatch, provider, old_base, new_base, failure):
    path = f"/api/settings/providers/{provider}"
    old_value = VALUE + "-old"
    saved = client.put(path, json={"api_key": old_value, "api_base": old_base}, headers=_header(SESSION))
    assert saved.status_code == 200

    def broken(*args, **kwargs):
        raise RuntimeError("settings write failed")

    with monkeypatch.context() as patched:
        if failure == "host":
            patched.setattr("api.providers.settings.save_setting_merge", broken)
        else:
            fail_the_commit_after_a_key_write(patched)
        with pytest.raises(RuntimeError, match="settings write failed|commit failed"):
            client.put(path, json={"api_key": VALUE, "api_base": new_base}, headers=_header(SESSION))

    assert key_store.resolve_provider(provider) == old_value
    assert (get_setting("providers") or {})[provider]["apiBase"] == old_base
    assert list(values_after_a_sweep().values()) == [old_value]


def test_network_provider_refuses_an_api_key_it_would_not_store(client):
    response = client.put(
        "/api/settings/providers/omlx",
        json={"api_key": VALUE, "api_base": "http://127.0.0.1:8000/v1"},
        headers=_header(SESSION),
    )

    assert response.status_code == 400
    assert "network-servers" in response.json()["detail"]
    assert key_store.resolve_provider("omlx") is None

    response = client.put(
        "/api/settings/providers/omlx", json={"api_base": "http://127.0.0.1:8000/v1"}, headers=_header(SESSION),
    )
    assert response.status_code == 200
    assert response.json()["has_key"] is False


@pytest.mark.parametrize("failure", ["host", "commit"])
def test_a_failed_first_write_leaves_no_key_behind(client, monkeypatch, failure):
    def broken(*args, **kwargs):
        raise RuntimeError("settings write failed")

    if failure == "host":
        monkeypatch.setattr("api.providers.settings.save_setting_merge", broken)
    else:
        fail_the_commit_after_a_key_write(monkeypatch)
    with pytest.raises(RuntimeError):
        client.put(
            "/api/settings/providers/openai",
            json={"api_key": VALUE, "api_base": "https://elsewhere.example/v1"}, headers=_header(SESSION),
        )

    assert key_store.resolve_provider("openai") is None
