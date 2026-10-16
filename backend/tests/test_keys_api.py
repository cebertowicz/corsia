"""Settings › Keys routes: who may change a key, the password on every write, and no echo.

Writes need a signed-in person's session from the header and the account
password in the same request. Three wrong passwords end that session only,
without touching sign-in's throttle, and parallel guesses cannot get past
three. Neither a value nor a password ever comes back in a response, a log or
the audit trail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN, OWNER_USER, TEST_PASSWORD, seed_account
from fastapi import FastAPI

from api import auth as auth_api
from api import key_write_guard
from api import keys as keys_api
from infra import accounts, key_store
from tools.web_search_outcome import KEY_REFUSED, SearchOutcome

VALUE = "BSAroutes-test-value-0123456789"
WRONG = "not-the-password-0123"
SECOND_SESSION = "second-session-credential-for-keys-tests"


@pytest.fixture
def client(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    # A lock binds to the first loop that waits on it; each test has its own loop.
    monkeypatch.setattr(key_write_guard, "_key_write_lock", asyncio.Lock())
    monkeypatch.setattr(key_write_guard, "_pending", {})
    seed_account(OWNER_USER, SECOND_SESSION, kind="session")
    app = FastAPI()
    app.include_router(keys_api.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


def _auth(token: str = OWNER_SESSION_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _values_bytes() -> bytes | None:
    path = key_store.keys_dir() / key_store.VALUES_FILE
    return path.read_bytes() if path.exists() else None


def _assert_never_carries(*texts: str) -> None:
    for text in texts:
        assert VALUE not in text
        assert TEST_PASSWORD not in text
        assert WRONG not in text


async def _set(client, password=TEST_PASSWORD, token=OWNER_SESSION_TOKEN, value=VALUE):
    return await client.put(
        "/api/keys/web-search/value", headers=_auth(token), json={"value": value, "password": password},
    )


@pytest.mark.asyncio
async def test_set_with_the_password_saves_and_never_shows_the_value(client, caplog):
    with caplog.at_level(logging.DEBUG):
        saved = await _set(client)
        listed = await client.get("/api/keys", headers=_auth())
    assert saved.status_code == 200, saved.text
    assert saved.json()["key"]["is_set"] is True
    assert listed.json()["keys"][0]["is_set"] is True
    _assert_never_carries(saved.text, listed.text, caplog.text)
    assert key_store.resolve_builtin(key_store.WEB_SEARCH)[0] == VALUE
    assert VALUE not in os.environ.values()


@pytest.mark.asyncio
async def test_mcp_key_write_reconnects_only_its_stable_app(client, monkeypatch):
    from connections.app_lifecycle import create_app
    from infra.database import get_db
    from infra.db_async import run_in_db
    from tools.mcp_secrets import entry_name
    import tools.connected_apps_runtime as connected_runtime

    def setup_app() -> tuple[dict[str, object], str]:
        db = get_db()
        try:
            app = create_app(
                "GitHub", {"command": "github", "key_refs": [{"kind": "env", "name": "GITHUB_TOKEN"}]},
                {"mode": "all", "project_ids": [], "outside_projects": False}, db=db,
            )
            key_name = entry_name(app["id"], "secret_env", "GITHUB_TOKEN")
            return app, key_name
        finally:
            db.close()

    app, key_name = await run_in_db(setup_app)
    entry = await run_in_db(key_store.entry_metadata, key_name)
    assert entry["name"] == key_name
    assert entry["label"] == "GitHub key"
    assert entry["is_set"] is False
    calls = []

    async def reconnect(action: str, app_id: str) -> dict[str, object]:
        calls.append((action, app_id))
        return {"state": "connected"}

    monkeypatch.setattr(connected_runtime, "apply_connected_app_runtime", reconnect)
    response = await client.put(
        f"/api/keys/{key_name}/value", headers=_auth(), json={"value": VALUE, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200, response.text
    assert calls == [("reconnect", app["id"])]

    def persisted_config_text() -> str:
        db = get_db()
        try:
            return "".join(
                row[0] for row in db.execute("SELECT config_json FROM connected_apps").fetchall()
            ) + "".join(row[0] for row in db.execute("SELECT value FROM settings").fetchall())
        finally:
            db.close()

    assert VALUE not in await run_in_db(persisted_config_text)


@pytest.mark.asyncio
async def test_mcp_key_write_saves_when_reconnect_is_rate_limited(client, monkeypatch):
    from connections.app_lifecycle import create_app
    from infra.database import get_db
    from infra.db_async import run_in_db
    from tools.mcp_secrets import entry_name
    from tools import mcp_runtime
    from tools.mcp_runtime import MCPReconnectRateLimitError
    import tools.connected_apps_runtime as connected_runtime

    def setup_app() -> tuple[dict[str, object], str]:
        db = get_db()
        try:
            app = create_app(
                "GitHub", {"command": "github", "key_refs": [{"kind": "env", "name": "GITHUB_TOKEN"}]},
                {"mode": "all", "project_ids": [], "outside_projects": False}, db=db,
            )
            return app, entry_name(app["id"], "secret_env", "GITHUB_TOKEN")
        finally:
            db.close()

    app, key_name = await run_in_db(setup_app)
    deferred = []

    async def rate_limited(action: str, app_id: str) -> dict[str, object]:
        raise MCPReconnectRateLimitError("This app was just reconnected; try again in 10 seconds.")

    async def defer(app_id: str) -> dict[str, object]:
        deferred.append(app_id)
        return {"state": "working", "state_since": "later", "tool_count": 1, "failure_class": None}

    monkeypatch.setattr(connected_runtime, "apply_connected_app_runtime", rate_limited)
    monkeypatch.setattr(mcp_runtime, "defer_connected_app_reconnect", defer, raising=False)

    response = await client.put(
        f"/api/keys/{key_name}/value", headers=_auth(), json={"value": VALUE, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200, response.text
    assert response.json()["key"]["is_set"] is True
    assert deferred == [app["id"]]


@pytest.mark.asyncio
async def test_set_leaves_the_settings_table_alone_and_a_refused_set_changes_nothing(client):
    from infra.settings import get_setting, set_setting

    tools = {"web": {"search": {"apiKey": "legacy-settings-value"}}}
    set_setting("tools", tools)
    refused = await _set(client, password=WRONG)
    assert refused.status_code == 403
    assert get_setting("tools") == tools
    saved = await _set(client)
    assert saved.status_code == 200, saved.text
    assert get_setting("tools") == tools
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) == (VALUE, key_store.SOURCE_STORE)


@pytest.mark.asyncio
async def test_writes_need_a_signed_in_person_not_an_api_credential(client):
    for token in (OWNER_TOKEN, "nobody"):
        refused = await _set(client, token=token)
        assert refused.status_code in (401, 403)
        _assert_never_carries(refused.text)
    # The query-string credential is not read for writes.
    query = await client.put(
        f"/api/keys/web-search/value?token={OWNER_SESSION_TOKEN}",
        json={"value": VALUE, "password": TEST_PASSWORD},
    )
    assert query.status_code == 401
    assert _values_bytes() is None


@pytest.mark.asyncio
async def test_refusals_never_echo_the_input(client):
    bad_value = await client.put(
        "/api/keys/web-search/value", headers=_auth(), json={"value": f"{VALUE} two", "password": TEST_PASSWORD},
    )
    malformed = await client.put(
        "/api/keys/web-search/value", headers=_auth(),
        content=json.dumps({"value": VALUE, "password": TEST_PASSWORD})[:-3],
    )
    not_object = await client.put("/api/keys/web-search/value", headers=_auth(), json=[VALUE, TEST_PASSWORD])
    no_password = await client.put("/api/keys/web-search/value", headers=_auth(), json={"value": VALUE})
    for response in (bad_value, malformed, not_object, no_password):
        assert response.status_code == 400
        _assert_never_carries(response.text)
    assert no_password.json()["detail"]["code"] == "password_required"


@pytest.mark.asyncio
async def test_a_username_in_the_body_is_ignored(client):
    seed_account("leksi", "leksi-session-for-keys", kind="session")
    # Leksi's session with the owner's username still checks Leksi's password.
    response = await client.put(
        "/api/keys/web-search/value", headers=_auth("leksi-session-for-keys"),
        json={"value": VALUE, "password": WRONG, "username": OWNER_USER},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "password_mismatch"


@pytest.mark.asyncio
async def test_a_wrong_password_changes_nothing_and_leaves_no_temporary(client):
    assert (await _set(client)).status_code == 200
    before = _values_bytes()
    refused = await _set(client, password=WRONG, value="BSAreplacement-0123456789")
    assert refused.status_code == 403
    assert refused.json()["detail"]["message"] == key_write_guard.MISMATCH
    assert _values_bytes() == before
    assert not list(key_store.keys_dir().glob("*.tmp"))


@pytest.mark.asyncio
async def test_no_value_or_password_reaches_the_database_file(client, isolated_db):
    from infra.database import get_db

    assert (await _set(client)).status_code == 200
    assert (await _set(client, password=WRONG)).status_code == 403
    get_db().execute("PRAGMA wal_checkpoint(FULL)")
    blob = b"".join(
        open(f"{isolated_db}{suffix}", "rb").read()
        for suffix in ("", "-wal") if os.path.exists(f"{isolated_db}{suffix}")
    )
    for secret in (VALUE, WRONG, TEST_PASSWORD):
        assert secret.encode() not in blob


@pytest.mark.asyncio
async def test_three_wrong_end_only_that_session_and_skip_the_sign_in_throttle(client, monkeypatch):
    throttle_before = dict(auth_api._failures)
    for attempt in range(2):
        assert (await _set(client, password=WRONG)).status_code == 403
    third = await _set(client, password=WRONG)
    assert third.status_code == 401
    assert third.json()["detail"]["message"] == key_write_guard.SIGNED_OUT
    assert accounts.resolve_token(OWNER_SESSION_TOKEN) is None
    assert accounts.resolve_token(SECOND_SESSION) is not None
    assert (await _set(client)).status_code == 401
    assert auth_api._failures == throttle_before


@pytest.mark.asyncio
async def test_a_correct_password_resets_the_count(client):
    for _ in range(2):
        assert (await _set(client, password=WRONG)).status_code == 403
    assert (await _set(client)).status_code == 200
    for _ in range(2):
        assert (await _set(client, password=WRONG)).status_code == 403
    assert accounts.resolve_token(OWNER_SESSION_TOKEN) is not None


@pytest.mark.asyncio
async def test_the_count_survives_a_restart(client, monkeypatch):
    for _ in range(2):
        assert (await _set(client, password=WRONG)).status_code == 403
    # A restart loses everything in memory; the count is in the database.
    monkeypatch.setattr(key_write_guard, "_pending", {})
    monkeypatch.setattr(key_write_guard, "_key_write_lock", asyncio.Lock())
    assert (await _set(client, password=WRONG)).status_code == 401
    assert accounts.resolve_token(OWNER_SESSION_TOKEN) is None


@pytest.mark.asyncio
async def test_parallel_guesses_hash_at_most_three_and_the_fourth_is_refused_first(client, monkeypatch):
    checks: list[str] = []
    real_check = accounts.check_password

    def counted(user_id, password, **kwargs):
        checks.append(user_id)
        return real_check(user_id, password, **kwargs)

    monkeypatch.setattr(key_write_guard.accounts, "check_password", counted)
    responses = await asyncio.gather(*(_set(client, password=WRONG) for _ in range(6)))
    codes = sorted(response.status_code for response in responses)
    assert len(checks) <= 3
    assert codes.count(429) >= 3
    assert 401 in codes
    assert accounts.resolve_token(OWNER_SESSION_TOKEN) is None
    for response in responses:
        _assert_never_carries(response.text)
    # Later requests on the ended session are refused.
    assert (await _set(client)).status_code == 401
    assert key_write_guard._pending == {}


@pytest.mark.asyncio
async def test_clear_and_remove_need_the_password(client):
    added = await client.post("/api/keys", headers=_auth(), json={"name": "maps", "label": "Maps"})
    assert added.status_code == 200, added.text
    assert (await client.post("/api/keys/maps/remove", headers=_auth(), json={"password": WRONG})).status_code == 403
    assert (await client.post("/api/keys/maps/remove", headers=_auth(), json={"password": TEST_PASSWORD})).json() == {
        "removed": "maps",
    }
    assert (await _set(client)).status_code == 200
    cleared = await client.post("/api/keys/web-search/clear", headers=_auth(), json={"password": TEST_PASSWORD})
    assert cleared.json()["key"]["is_set"] is False


@pytest.mark.asyncio
async def test_check_reports_the_class_to_any_signed_in_caller(client, monkeypatch):
    async def refused(api_key, query, count):
        assert api_key == VALUE
        return SearchOutcome(KEY_REFUSED)

    monkeypatch.setattr(keys_api, "run_search", refused)
    nothing = await client.post("/api/keys/web-search/check", headers=_auth())
    assert nothing.json() == {"outcome": "not_set", "reason": None}
    await _set(client)
    checked = await client.post("/api/keys/web-search/check", headers=_auth())
    assert checked.json() == {"outcome": KEY_REFUSED, "reason": None}
    # A conductor checks the key the person saved; the result never carries the value.
    by_token = await client.post("/api/keys/web-search/check", headers=_auth(OWNER_TOKEN))
    assert by_token.json() == {"outcome": KEY_REFUSED, "reason": None}
    assert (await client.post("/api/keys/maps/check", headers=_auth())).status_code == 404


@pytest.mark.asyncio
async def test_adding_refuses_a_name_in_use(client):
    added = await client.post(
        "/api/keys", headers=_auth(), json={"name": "maps", "label": "Maps", "description": "Map tiles"},
    )
    assert added.json()["key"]["label"] == "Maps"
    taken = await client.post("/api/keys", headers=_auth(), json={"name": "web-search", "label": "Search"})
    assert taken.status_code == 400
    assert "already exists" in taken.json()["detail"]["message"]


@pytest.mark.parametrize("name, kind", [
    ("web-search", "builtin"),
    ("provider-anthropic", "provider"),
    ("server-ns-live1", "server"),
    ("mcp-svc-0123456789", "mcp"),
    ("maps", "added"),
    ("provider-unknown", "added"),
])
def test_the_audit_kind_names_what_the_entry_belongs_to(name, kind):
    assert keys_api._kind(name) == kind
