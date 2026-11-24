"""No Settings › Keys route can see or touch a saved login's entry.

The keys list is exactly what it was before a login was saved. Adding an entry
under the login prefix, or by a login entry's own name, is refused; setting,
clearing and removing a login entry are refused even with the account
password, and leave its password in place. No refusal carries the password.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN, TEST_PASSWORD
from fastapi import FastAPI

from api import key_write_guard
from api import keys as keys_api
from infra import saved_logins
from infra.db_async import run_in_db

PASSWORD = "a login password with spaces ü 0123456789"


@pytest.fixture
def client(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setattr(key_write_guard, "_key_write_lock", asyncio.Lock())
    monkeypatch.setattr(key_write_guard, "_pending", {})
    app = FastAPI()
    app.include_router(keys_api.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _auth(token: str = OWNER_SESSION_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _save_login() -> tuple[str, str]:
    def save() -> str:
        return saved_logins.save_login(
            site="https://example.com", username="me@example.com", password=PASSWORD, saved_by="person",
        )

    login_id = await run_in_db(save)
    return login_id, saved_logins.entry_name(login_id)


async def _password_still_there(login_id: str) -> None:
    assert await run_in_db(saved_logins.password_for_fill, login_id) == PASSWORD


@pytest.mark.asyncio
async def test_the_keys_list_is_exactly_what_it_was_before_a_login_was_saved(client):
    before = await client.get("/api/keys", headers=_auth(OWNER_TOKEN))
    login_id, name = await _save_login()
    after = await client.get("/api/keys", headers=_auth(OWNER_TOKEN))
    assert before.status_code == after.status_code == 200
    assert after.json() == before.json()
    assert name not in after.text and login_id not in after.text and PASSWORD not in after.text


@pytest.mark.asyncio
async def test_an_entry_cannot_be_added_under_a_login_entry_name(client):
    _login_id, name = await _save_login()
    exact = await client.post("/api/keys", headers=_auth(OWNER_TOKEN), json={"name": name, "label": "Mine"})
    assert exact.status_code == 400
    assert PASSWORD not in exact.text


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", ["saved-login-", "saved-login-github", "saved-login--x", "saved-login-0123456789"])
async def test_no_entry_can_claim_the_login_prefix(client, claimed):
    refused = await client.post("/api/keys", headers=_auth(OWNER_TOKEN), json={"name": claimed, "label": "Mine"})
    assert refused.status_code == 400
    assert refused.json()["detail"]["message"] == "A key with that name already exists."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("value", {"value": "replaced-0123456789", "password": TEST_PASSWORD}),
        ("clear", {"password": TEST_PASSWORD}),
        ("remove", {"password": TEST_PASSWORD}),
    ],
)
async def test_a_login_entry_cannot_be_set_cleared_or_removed(client, path, body):
    login_id, name = await _save_login()
    method = client.put if path == "value" else client.post
    refused = await method(f"/api/keys/{name}/{path}", headers=_auth(), json=body)
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["message"] == "No key has that name."
    assert PASSWORD not in refused.text
    await _password_still_there(login_id)
    assert (await run_in_db(saved_logins.get_login, login_id)).username == "me@example.com"


@pytest.mark.asyncio
async def test_a_login_entry_has_no_check(client):
    _login_id, name = await _save_login()
    refused = await client.post(f"/api/keys/{name}/check", headers=_auth(OWNER_TOKEN))
    assert refused.status_code == 404
