"""Microsoft M1 provider-fake coverage: public-client PKCE, identity, refresh and local removal."""
from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from connections.app_reach import AppReach
from connections.microsoft import client, oauth
from connections.microsoft.accounts import microsoft_accounts, resolve_microsoft_credentials
from connections.microsoft.account_actions import remove_microsoft_account
from connections.microsoft.errors import INPUT_REFUSAL, REFRESH_TOKEN_INVALID, MicrosoftError
from infra.database import get_db

CLIENT_ID = "11111111-1111-4111-8111-111111111111"
TENANT_A = "22222222-2222-4222-8222-222222222222"
TENANT_B = "33333333-3333-4333-8333-333333333333"
REFRESH = "refresh-token-canary"
ACCESS = "access-token-canary"
_REAL_CLIENT = httpx.AsyncClient


def _token(tenant: str, object_id: str, address: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"tid": tenant, "oid": object_id, "preferred_username": address}).encode()).rstrip(b"=").decode()
    return f"head.{payload}.signature"


class FakeMicrosoft:
    def __init__(self) -> None:
        self.refresh_token = REFRESH
        self.invalid_refresh = False
        self.token_error = False
        self.forms: list[dict[str, str]] = []
        self.identity = _token(TENANT_A, "object-a", "same@example.test")

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()} if request.method == "POST" else {}
        self.forms.append(form)
        if form.get("grant_type") == "refresh_token":
            if self.invalid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant", "unsafe": REFRESH})
            return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": "rotated-refresh"})
        if self.token_error:
            return httpx.Response(400, json={"error": "invalid_grant", "unsafe": "token-error-canary"})
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": self.refresh_token, "id_token": self.identity})


@pytest.fixture
def microsoft(monkeypatch):
    fake = FakeMicrosoft()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(oauth.httpx, "AsyncClient", factory)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(oauth, "_pending", {})
    return fake


async def _callback(redirect_uri: str, *, path: str | None = None, **query: str) -> tuple[int, bytes]:
    parsed = urlparse(redirect_uri)
    assert parsed.hostname == "localhost" and parsed.port is not None
    reader, writer = await asyncio.open_connection("127.0.0.1", parsed.port)
    target = (path or parsed.path) + "?" + urlencode(query)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost:{parsed.port}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 2)
    writer.close()
    await writer.wait_closed()
    return int(response.split(b" ", 2)[1]), response


async def _connect(fake: FakeMicrosoft, *, kind: str = "work", tenant: str | None = TENANT_A) -> str:
    url, account_id = await oauth.start_sign_in(CLIENT_ID, kind, tenant, actor=("system", "test"), reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [" ".join(oauth.SCOPES)]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"][0].startswith("http://localhost:")
    assert "client_secret" not in query
    status, _page = await _callback(query["redirect_uri"][0], code="authorization-code", state=query["state"][0])
    assert status == 200
    return account_id


async def test_pkce_authority_and_guid_input_refusal(microsoft, isolated_db) -> None:
    url, account_id = await oauth.start_sign_in(CLIENT_ID, "personal", actor=("system", "test"), reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))
    assert url.startswith("https://login.microsoftonline.com/consumers/")
    with pytest.raises(MicrosoftError, match=INPUT_REFUSAL):
        await oauth.start_sign_in("not-a-guid", "work", TENANT_A, actor=("system", "test"), reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))
    with pytest.raises(MicrosoftError, match=INPUT_REFUSAL):
        await oauth.start_sign_in(CLIENT_ID, "work", actor=("system", "test"), reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))
    with pytest.raises(MicrosoftError, match=INPUT_REFUSAL):
        await oauth.start_sign_in(CLIENT_ID, "personal", TENANT_A, actor=("system", "test"), reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))
    await oauth.clear_pending(account_id)


async def test_loopback_callback_uses_its_own_port_and_refuses_wrong_state(microsoft, isolated_db) -> None:
    url, _ = await oauth.start_sign_in(
        CLIENT_ID, "work", TENANT_A, actor=("system", "test"),
        reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False),
    )
    query = parse_qs(urlparse(url).query)
    redirect_uri, state = query["redirect_uri"][0], query["state"][0]
    pending = oauth._pending[state]
    assert urlparse(redirect_uri).port == pending.listener.port
    assert pending.listener.bound_addresses() == ["127.0.0.1"]
    wrong_path_status, _ = await _callback(redirect_uri, path="/not-microsoft", code="not-used", state=state)
    assert wrong_path_status == 404
    assert state in oauth._pending
    wrong_state_status, _ = await _callback(redirect_uri, code="not-used", state="wrong-state")
    assert wrong_state_status == 400
    assert state in oauth._pending
    assert microsoft.forms == []
    success_status, page = await _callback(redirect_uri, code="authorization-code", state=state)
    assert success_status == 200
    assert b"Microsoft is connected. You can close this tab and go back to CORSIA." in page
    assert b"authorization-code" not in page
    assert not oauth._pending
    assert pending.listener.bound_addresses() == []
    assert microsoft.forms[-1]["code"] == "authorization-code"


async def test_callback_failure_page_never_echoes_code_or_provider_detail(microsoft, isolated_db) -> None:
    microsoft.token_error = True
    url, _ = await oauth.start_sign_in(
        CLIENT_ID, "work", TENANT_A, actor=("system", "test"),
        reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False),
    )
    query = parse_qs(urlparse(url).query)
    status, page = await _callback(
        query["redirect_uri"][0], code="authorization-code-canary", state=query["state"][0],
    )
    assert status == 400
    assert b"Microsoft sign-in couldn&#x27;t finish. Go back to CORSIA and try again." in page
    assert b"authorization-code-canary" not in page
    assert b"token-error-canary" not in page
    assert not oauth._pending


async def test_timeout_closes_pending_loopback_listener(microsoft, isolated_db, monkeypatch) -> None:
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0.01)
    url, _ = await oauth.start_sign_in(
        CLIENT_ID, "work", TENANT_A, actor=("system", "test"),
        reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False),
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    listener = oauth._pending[state].listener
    await asyncio.sleep(0.05)
    assert not oauth._pending
    assert listener.bound_addresses() == []


async def test_shutdown_closes_pending_loopback_listener(microsoft, isolated_db) -> None:
    url, _ = await oauth.start_sign_in(
        CLIENT_ID, "work", TENANT_A, actor=("system", "test"),
        reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False),
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    listener = oauth._pending[state].listener
    await oauth.stop_all_signins()
    assert not oauth._pending
    assert listener.bound_addresses() == []


def test_microsoft_backend_has_no_fixed_callback_route() -> None:
    from api.microsoft import router

    assert "/oauth/callback" not in {route.path for route in router.routes}


async def test_same_address_in_two_tenants_has_two_rows(microsoft, isolated_db) -> None:
    first = await _connect(microsoft)
    microsoft.identity = _token(TENANT_B, "object-b", "same@example.test")
    second = await _connect(microsoft, tenant=TENANT_B)
    assert first != second
    db = get_db()
    try:
        rows = microsoft_accounts(db=db)
        assert {row["provider_subject"] for row in rows} == {f"{TENANT_A}:object-a", f"{TENANT_B}:object-b"}
        assert {row["address"] for row in rows} == {"same@example.test"}
        assert {row["account_kind"] for row in rows} == {"work"}
    finally:
        db.close()


async def test_refresh_replaces_token_and_failure_marks_only_that_row(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)
    assert await client.access_token(account_id) == ACCESS
    db = get_db()
    try:
        assert resolve_microsoft_credentials(account_id, db=db)["refresh_token"] == "rotated-refresh"
    finally:
        db.close()
    microsoft.invalid_refresh = True
    with pytest.raises(MicrosoftError, match=REFRESH_TOKEN_INVALID):
        await client.access_token(account_id)
    db = get_db()
    try:
        [row] = microsoft_accounts(db=db)
        assert row["state"] == "needs_sign_in"
        assert row["state_cause"] == REFRESH_TOKEN_INVALID
    finally:
        db.close()


async def test_remove_deletes_row_and_pending_without_session_revocation(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, account_id=account_id, actor=("system", "test"))
    state = parse_qs(urlparse(url).query)["state"][0]
    listener = oauth._pending[state].listener
    result = await remove_microsoft_account(account_id, actor=("person", "person"))
    assert result["revocation"] == "manual_only"
    db = get_db()
    try:
        assert microsoft_accounts(db=db) == []
    finally:
        db.close()
    assert not oauth._pending
    assert listener.bound_addresses() == []


async def test_status_contract_and_setup_helper(microsoft, isolated_db) -> None:
    from connections.microsoft.accounts import has_account

    assert oauth.accounts_status() == {"status": "not_connected"}
    assert not has_account()
    account_id = await _connect(microsoft)
    assert has_account()
    one = oauth.accounts_status(account_id=account_id)
    assert one["account_id"] == account_id
    assert one["address"] == "same@example.test"
    assert one["kind"] == "work"
    assert one["state"] == "connected"


def test_microsoft_tools_are_in_the_microsoft_pack() -> None:
    from tools.catalog import TOOL_PACKS
    from tools.catalog_entries import TOOL_CATALOG
    from tools.microsoft_account import MicrosoftConnectTool, MicrosoftStatusTool

    assert TOOL_PACKS["microsoft"] == "Microsoft sign-in"
    assert {MicrosoftConnectTool().name, MicrosoftStatusTool().name} <= set(TOOL_CATALOG)
    assert TOOL_CATALOG["microsoft_connect"].category == TOOL_CATALOG["microsoft_status"].category == "microsoft"


def test_microsoft_tool_contract_is_pinned() -> None:
    from tools.microsoft_account import MicrosoftConnectTool, MicrosoftStatusTool

    connect = MicrosoftConnectTool()
    status = MicrosoftStatusTool()
    assert connect.name == "microsoft_connect"
    assert connect.parameters == {
        "type": "object",
        "additionalProperties": False,
        "required": ["client_id", "kind"],
        "properties": {
            "client_id": {"type": "string", "description": "Microsoft Application (client) ID GUID."},
            "kind": {"type": "string", "enum": ["personal", "work", "both"]},
            "tenant_id": {"type": "string", "description": "Directory (tenant) ID GUID, required for work."},
        },
    }
    assert status.name == "microsoft_status"
    assert status.parameters == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"account_id": {"type": "string", "description": "Optional account_id returned by microsoft_connect or a previous microsoft_status result."}},
    }
    assert connect.self_describing_errors and status.self_describing_errors


# Person-only Sign out and Remove share ``api.connected_account_rows`` with Google;
# that route's direct control coverage remains in test_connected_account_rows.py.
