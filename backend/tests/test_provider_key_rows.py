"""Settings › Keys lists provider keys as status rows, and notes ignored environment keys.

A provider row says whether a key is set and never carries it. A legacy
provider appears only while it holds a key and can be removed there with the
password. A key environment variable CORSIA ignores is noted by presence alone.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, TEST_PASSWORD, seed_account
from fastapi import FastAPI

from api import key_write_guard
from api import keys as keys_api
from infra import ignored_environment, key_store, key_store_providers

VALUE = "BSArows-test-value-0123456789"


@pytest.fixture
def client(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(key_write_guard, "_key_write_lock", asyncio.Lock())
    monkeypatch.setattr(key_write_guard, "_pending", {})
    monkeypatch.setattr(ignored_environment, "_present", frozenset())
    app = FastAPI()
    app.include_router(keys_api.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _auth() -> dict:
    return {"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"}


@pytest.mark.asyncio
async def test_current_providers_are_listed_set_or_not_and_legacy_ones_only_when_set(client):
    key_store_providers.set_provider_key("anthropic", VALUE)
    body = (await client.get("/api/keys", headers=_auth())).json()
    rows = {row["provider"]: row for row in body["provider_keys"]}
    assert set(rows) == {"anthropic", "openai"}
    assert rows["anthropic"] == {
        "name": "provider-anthropic", "provider": "anthropic", "label": "Anthropic",
        "is_set": True, "unsupported": False, "ignored_env_var": None,
    }
    assert rows["openai"]["is_set"] is False
    assert VALUE not in str(body)

    key_store_providers.set_provider_key("groq", VALUE)
    rows = {row["provider"]: row for row in (await client.get("/api/keys", headers=_auth())).json()["provider_keys"]}
    assert rows["groq"]["is_set"] is True and rows["groq"]["unsupported"] is True
    assert [e["name"] for e in (await client.get("/api/keys", headers=_auth())).json()["keys"]] == [
        "web-search", "github-webhook",
    ]


@pytest.mark.asyncio
async def test_a_legacy_provider_key_is_removed_in_keys_with_the_password(client):
    key_store_providers.set_provider_key("groq", VALUE)
    url = "/api/keys/provider-groq/remove"
    assert (await client.post(url, headers=_auth(), json={})).status_code == 400
    assert key_store_providers.provider_keys()["groq"] == VALUE
    removed = await client.post(url, headers=_auth(), json={"password": TEST_PASSWORD})
    assert removed.status_code == 200
    assert "groq" not in key_store_providers.provider_keys()
    rows = (await client.get("/api/keys", headers=_auth())).json()["provider_keys"]
    assert "groq" not in {row["provider"] for row in rows}


@pytest.mark.asyncio
async def test_a_provider_key_cannot_be_set_or_cleared_as_if_it_were_an_added_key(client):
    for path, body in (("/value", {"value": VALUE}), ("/clear", {})):
        method = client.put if path == "/value" else client.post
        response = await method(
            f"/api/keys/provider-anthropic{path}", headers=_auth(), json={**body, "password": TEST_PASSWORD},
        )
        assert response.status_code == 400
    assert key_store_providers.provider_keys() == {}


@pytest.mark.asyncio
async def test_the_github_webhook_secret_is_a_row_that_cannot_be_checked(client):
    body = (await client.get("/api/keys", headers=_auth())).json()
    hook = next(e for e in body["keys"] if e["name"] == "github-webhook")
    assert (hook["builtin"], hook["checkable"], hook["is_set"]) == (True, False, False)
    assert (await client.post("/api/keys/github-webhook/check", headers=_auth())).status_code == 404


@pytest.mark.asyncio
async def test_only_presence_of_an_unread_environment_variable_is_noted(client, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-value-not-to-be-read")
    monkeypatch.setenv("BRAVE_API_KEY", "other-environment-value")
    ignored_environment.note_ignored_environment()
    body = (await client.get("/api/keys", headers=_auth())).json()

    assert {e["name"]: e["ignored_env_var"] for e in body["keys"]} == {
        "web-search": "BRAVE_API_KEY", "github-webhook": None,
    }
    assert {r["provider"]: r["ignored_env_var"] for r in body["provider_keys"]} == {
        "anthropic": "ANTHROPIC_API_KEY", "openai": None,
    }
    assert "environment-value" not in str(body)
    assert ignored_environment._present == {"provider-anthropic", "web-search"}
    assert key_store.resolve_provider("anthropic") is None
