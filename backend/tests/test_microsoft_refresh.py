"""Microsoft Graph refresh uses shared account credentials and row-local lifecycle state."""
from __future__ import annotations

import inspect
import time
import uuid

import pytest

from connections.account_actions import sign_out_account
from connections.connected_accounts import ConnectedAccountError, create_account, get_account, resolve_credentials, store_credentials
from connections.microsoft import client
from connections.microsoft.errors import MicrosoftError, REFRESH_TOKEN_INVALID, SERVICE_UNAVAILABLE, message
from connections.microsoft.provider import MICROSOFT
from infra.database import get_db
from tools import microsoft_actions


REACH = {"mode": "all", "project_ids": [], "outside_projects": False}


def connect(tenant="consumers"):
    account = str(create_account(
        MICROSOFT.name, "mail@example.test", reach=REACH, created_by=("system", "test"),
        account_kind="personal" if tenant == "consumers" else "work", provider_subject=f"{tenant}:{uuid.uuid4()}",
    )["id"])
    store_credentials(account, {
        "access_token": "old-access", "access_token_expires_at": "0", "refresh_token": "old-refresh", "client_id": "client",
        "tenant_id": tenant, "scopes": ["User.Read", "Mail.Read"],
    })
    return account


def facts(account):
    db = get_db()
    try:
        return get_account(account, db=db), resolve_credentials(account, db=db)
    finally:
        db.close()


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class Http:
    def __init__(self, response, calls, before=None):
        self.response, self.calls, self.before = response, calls, before

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.before:
            result = self.before()
            if inspect.isawaitable(result):
                await result
        return self.response


@pytest.mark.parametrize("tenant", ["consumers", "11111111-1111-4111-8111-111111111111"])
async def test_refresh_rotates_shared_bundle_using_recorded_tenant(isolated_db, monkeypatch, tenant):
    account, other = connect(tenant), connect()
    calls = []
    http = Http(Response(200, {"access_token": "new-access", "refresh_token": "new-refresh"}), calls)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    assert await client.access_token(account) == "new-access"
    assert calls[0][0] == client.authority_url(tenant)
    assert "/common/" not in calls[0][0]
    assert calls[0][1]["data"]["refresh_token"] == "old-refresh"
    row, bundle = facts(account)
    assert row["state"] == "connected"
    assert bundle["refresh_token"] == "new-refresh" and bundle["access_token"] == "new-access"
    assert facts(other)[1]["refresh_token"] == "old-refresh"


async def test_refresh_cache_is_reused_and_legacy_bundle_refreshes_once(isolated_db, monkeypatch):
    account = connect()
    calls = []
    http = Http(Response(200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}), calls)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)

    assert await client.access_token(account) == "new-access"
    assert await client.access_token(account) == "new-access"
    assert len(calls) == 1

    legacy = facts(account)[1]
    legacy.pop("access_token_expires_at")
    store_credentials(account, legacy)
    calls.clear()
    assert await client.access_token(account) == "new-access"
    assert len(calls) == 1


@pytest.mark.parametrize("error_name", ["invalid_grant", "interaction_required", "invalid_client", "unauthorized_client"])
async def test_failed_refresh_marks_only_refused_row_with_fixed_cause(isolated_db, monkeypatch, error_name):
    account, other = connect(), connect()
    http = Http(Response(400, {"error": error_name, "error_description": "private provider text"}), [])
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    with pytest.raises(MicrosoftError) as error:
        await client.access_token(account)
    assert error.value.cause == REFRESH_TOKEN_INVALID
    assert "private provider text" not in str(error.value)
    assert facts(account)[0]["state"] == "needs_sign_in"
    assert facts(account)[0]["state_cause"] == REFRESH_TOKEN_INVALID
    assert facts(other)[0]["state"] == "connected" and facts(other)[0]["state_cause"] is None


async def test_late_refresh_failure_cannot_invalidate_replacement(isolated_db, monkeypatch):
    account = connect()

    def replace():
        bundle = facts(account)[1]
        store_credentials(account, {**bundle, "refresh_token": "replacement"})

    http = Http(Response(400, {"error": "invalid_grant"}), [], before=replace)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    with pytest.raises(MicrosoftError, match=REFRESH_TOKEN_INVALID):
        await client.access_token(account)
    row, bundle = facts(account)
    assert row["state"] == "connected" and row["state_cause"] is None
    assert bundle["refresh_token"] == "replacement"


async def test_sign_out_refuses_refresh_before_provider(isolated_db, monkeypatch):
    account = connect()
    await sign_out_account(account, actor=("person", "test"))
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: pytest.fail("signed-out account cannot refresh"))
    with pytest.raises(MicrosoftError, match=REFRESH_TOKEN_INVALID):
        await client.access_token(account)


async def test_refresh_finishing_after_sign_out_cannot_restore_credentials(isolated_db, monkeypatch):
    account = connect()

    async def sign_out():
        await sign_out_account(account, actor=("person", "test"))

    http = Http(Response(200, {"access_token": "late-access", "refresh_token": "late-refresh"}), [], before=sign_out)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    with pytest.raises(MicrosoftError, match=REFRESH_TOKEN_INVALID):
        await client.access_token(account)
    row, bundle = facts(account)
    assert row["state"] == "signed_out" and bundle is None


async def test_transient_refresh_failure_keeps_connected_state(isolated_db, monkeypatch):
    account = connect()
    http = Http(Response(503, {"error": "private provider text"}), [])
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    with pytest.raises(MicrosoftError, match=SERVICE_UNAVAILABLE):
        await client.access_token(account)
    assert facts(account)[0]["state"] == "connected"


@pytest.mark.parametrize("error_name", ["invalid_scope", "invalid_request"])
async def test_a_permanent_refresh_refusal_offers_sign_in(isolated_db, monkeypatch, error_name):
    """A refusal Microsoft will keep giving is the account's state, not a wait-a-minute retry."""
    account, other = connect(), connect()
    http = Http(Response(400, {"error": error_name, "error_description": "private provider text"}), [])
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    with pytest.raises(MicrosoftError) as error:
        await client.access_token(account)
    assert error.value.cause == REFRESH_TOKEN_INVALID
    assert "private provider text" not in str(error.value)
    assert facts(account)[0]["state"] == "needs_sign_in"
    assert facts(account)[0]["state_cause"] == REFRESH_TOKEN_INVALID
    assert facts(other)[0]["state"] == "connected" and facts(other)[0]["state_cause"] is None


@pytest.mark.parametrize("status, error_name", [
    (503, "server_error"), (429, "server_busy"), (400, "temporarily_unavailable"),
])
async def test_only_an_outage_or_throttle_keeps_the_account_connected(isolated_db, monkeypatch, status, error_name):
    account = connect()
    http = Http(Response(status, {"error": error_name, "error_description": "private provider text"}), [])
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: http)
    with pytest.raises(MicrosoftError) as error:
        await client.access_token(account)
    assert error.value.cause == SERVICE_UNAVAILABLE
    assert facts(account)[0]["state"] == "connected" and facts(account)[0]["state_cause"] is None


def test_a_retry_tells_the_person_not_to_start_a_sign_in():
    """The retry text is the only thing standing between a throttled account and a needless sign-in."""
    text = message(SERVICE_UNAVAILABLE)
    assert "Microsoft didn’t complete this request" in text and "Don’t start a new sign-in" in text
    assert "didn't" not in text and "Don't" not in text


def test_microsoft_account_resolution_refuses_google_unknown_and_unreached_alike(isolated_db):
    from tests.event_routing_helpers import mk_session

    thread = mk_session("graph-caller")
    account = connect()
    assert microsoft_actions._visible_account(account, thread)["id"] == account
    google = str(create_account("google", "google@example.test", reach=REACH, created_by=("system", "test"))["id"])
    unreachable = str(create_account(
        MICROSOFT.name, "hidden@example.test", reach={"mode": "selected", "project_ids": [], "outside_projects": False},
        created_by=("system", "test"), account_kind="personal", provider_subject="consumers:hidden",
    )["id"])
    errors = []
    for selector in (google, unreachable, "unknown"):
        with pytest.raises(ConnectedAccountError) as error:
            microsoft_actions._visible_account(selector, thread)
        errors.append(str(error.value))
    assert errors == ["Microsoft account is unavailable to this thread"] * 3


async def test_mail_draft_resolves_sender_through_shared_visibility(isolated_db, monkeypatch):
    import json

    from infra import config
    from tests.event_routing_helpers import mk_session

    thread, account = mk_session("draft-caller"), connect()

    async def designated(_kwargs):
        return None

    monkeypatch.setattr(microsoft_actions, "_designated", designated)
    result = json.loads(await microsoft_actions.MicrosoftMailDraftTool().execute(
        _thread_id=thread, account_id=account, to=["recipient@example.test"], subject="Review me", body="Draft body",
    ))
    assert result["account_id"] == account and result["draft_id"]
    document = (config.WORKSPACE / result["path"]).read_text()
    assert "mail@example.test" in document and "Draft body" in document


class GraphHttp:
    """Graph refuses the cached token once; the token endpoint issues a new one."""

    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append("refresh")
        return Response(200, {"access_token": "renewed-access", "expires_in": 3600})

    async def request(self, method, url, **kwargs):
        bearer = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        self.calls.append(bearer)
        return Response(401 if bearer == "cached-access" else 200, {"id": "ok"})


async def test_graph_401_expires_the_cached_token_in_the_real_store_then_retries(isolated_db, monkeypatch):
    account = connect()
    store_credentials(account, {
        **facts(account)[1], "access_token": "cached-access", "access_token_expires_at": str(time.time() + 3600),
    })
    calls = []
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: GraphHttp(calls))
    assert await client.graph_json("GET", "item", account_id=account) == {"id": "ok"}
    assert calls == ["cached-access", "refresh", "renewed-access"]
    row, bundle = facts(account)
    assert row["state"] == "connected" and row["state_cause"] is None
    assert bundle["access_token"] == "renewed-access"


async def test_graph_401_leaves_a_newer_token_alone(isolated_db):
    account = connect()
    store_credentials(account, {**facts(account)[1], "access_token": "newer-access", "access_token_expires_at": "9999999999"})
    await client.run_in_db(client._on_account, client._drop_cached_access_token, account, "older-access")
    bundle = facts(account)[1]
    assert (bundle["access_token"], bundle["access_token_expires_at"]) == ("newer-access", "9999999999")
