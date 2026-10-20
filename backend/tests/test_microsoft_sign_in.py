"""Microsoft provider-fake coverage: public-client PKCE, Graph identity, sign-in again and the shared rows."""
from __future__ import annotations

import asyncio
import html
import json
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from api.connected_account_rows import list_account_rows
from connections import account_actions, connected_accounts
from connections.app_reach import AppReach
from connections.connected_accounts import (
    GOOGLE_PROVIDER, MICROSOFT_PROVIDER, create_account, list_accounts, resolve_credentials, store_credentials,
)
from connections.microsoft import account_store, client, oauth
from connections.microsoft.errors import (
    ACCOUNT_NOT_FOUND, ADMIN_APPROVAL_REQUIRED, CONSENT_CANCELLED, DIFFERENT_ACCOUNT, INPUT_REFUSAL, INTERNAL,
    PROVIDER_UNAVAILABLE, REFRESH_TOKEN_INVALID, REGISTRATION_MISCONFIGURED, MicrosoftError, exchange_error_cause,
    message,
)
from google_account_helpers import make_only_corsia
from infra.database import get_db

CLIENT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_CLIENT = "44444444-4444-4444-8444-444444444444"
TENANT_A = "22222222-2222-4222-8222-222222222222"
TENANT_B = "33333333-3333-4333-8333-333333333333"
REFRESH = "refresh-token-canary"
ACCESS = "access-token-canary"
EVERYWHERE = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)
PERSON = ("person", "person")
_REAL_CLIENT = httpx.AsyncClient


class FakeMicrosoft:
    """The identity platform's token endpoint and Graph ``/me`` and ``/organization``."""

    def __init__(self) -> None:
        self.token_error = False
        self.me: dict[str, object] = {"id": "object-a", "mail": "same@example.test"}
        self.me_status = 200
        self.organization_status = 200
        self.organizations: list[dict[str, str]] = [{"id": TENANT_A}]
        self.organization_error: dict[str, object] = {"error": {"code": "Error", "message": "graph-canary"}}
        self.forms: list[dict[str, str]] = []
        self.token_urls: list[str] = []
        self.graph_auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.graph_auth.append(request.headers.get("Authorization", ""))
            if request.url.path.endswith("/me"):
                return httpx.Response(self.me_status, json=self.me if self.me_status == 200 else {"error": "graph-canary"})
            if request.url.path.endswith("/organization"):
                body = {"value": self.organizations} if self.organization_status == 200 else self.organization_error
                return httpx.Response(self.organization_status, json=body)
            return httpx.Response(404)
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        self.token_urls.append(str(request.url))
        if self.token_error:
            return httpx.Response(400, json={"error": "invalid_grant", "unsafe": "token-error-canary"})
        # No id_token: the identity comes from Graph alone.
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer"})


@pytest.fixture
def microsoft(monkeypatch):
    fake = FakeMicrosoft()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(oauth.httpx, "AsyncClient", factory)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(oauth, "_pending", {})
    monkeypatch.setattr(oauth, "_latest_add_failures", {})
    return fake


@pytest.fixture
def agent_adds(monkeypatch):
    """Let a test thread add an account without seeding its project scope."""
    monkeypatch.setattr(connected_accounts, "reach_for_adding_thread", lambda _thread_id, *, db: EVERYWHERE)


async def _callback(redirect_uri: str, *, path: str | None = None, **query: str) -> tuple[int, bytes]:
    parsed = urlparse(redirect_uri)
    assert parsed.hostname == "localhost" and parsed.port is not None
    reader, writer = await asyncio.open_connection("127.0.0.1", parsed.port)
    target = (path if path is not None else parsed.path or "/") + "?" + urlencode(query)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost:{parsed.port}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 2)
    writer.close()
    await writer.wait_closed()
    return int(response.split(b" ", 2)[1]), response


def _query(url: str) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}


def _page_has(page: bytes, text: str) -> bool:
    return html.escape(text).encode() in page or text.encode() in page


async def _finish(url: str, **query: str) -> tuple[int, bytes]:
    found = _query(url)
    return await _callback(found["redirect_uri"], state=found["state"], **(query or {"code": "authorization-code"}))


async def _connect(fake: FakeMicrosoft, *, kind: str = "work", tenant: str | None = TENANT_A) -> str:
    url, account_id = await oauth.start_sign_in(CLIENT_ID, kind, tenant, actor=("system", "test"), reach=EVERYWHERE)
    query = _query(url)
    assert query["scope"] == " ".join(oauth.SCOPES)
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == f"http://localhost:{oauth._pending[query['state']].listener.port}"
    assert "client_secret" not in query
    status, _page = await _finish(url)
    assert status == 200
    assert fake.forms[-1]["redirect_uri"] == query["redirect_uri"]
    return account_id


def _accounts() -> list[dict[str, object]]:
    db = get_db()
    try:
        return list_accounts(MICROSOFT_PROVIDER, db=db)
    finally:
        db.close()


def _bundle(account_id: str) -> dict[str, object] | None:
    db = get_db()
    try:
        return resolve_credentials(account_id, db=db)
    finally:
        db.close()


def test_scopes_are_exactly_the_graph_ones_without_openid_or_profile() -> None:
    assert oauth.SCOPES == ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite", "Files.ReadWrite", "offline_access", "User.Read")


async def test_pkce_authority_and_guid_input_refusal(microsoft, isolated_db) -> None:
    url, account_id = await oauth.start_sign_in(CLIENT_ID, "personal", actor=("system", "test"), reach=EVERYWHERE)
    assert url.startswith("https://login.microsoftonline.com/consumers/")
    for args in (("not-a-guid", "work", TENANT_A), (CLIENT_ID, "work", None), (CLIENT_ID, "personal", TENANT_A)):
        with pytest.raises(MicrosoftError, match=INPUT_REFUSAL) as refused:
            await oauth.start_sign_in(*args, actor=("system", "test"), reach=EVERYWHERE)
        assert refused.value.message == message(INPUT_REFUSAL)
    await oauth.clear_pending(account_id)


def test_every_failure_is_a_fixed_code_with_a_real_message() -> None:
    with pytest.raises(ValueError):
        MicrosoftError("provider says no")
    error = MicrosoftError(ADMIN_APPROVAL_REQUIRED)
    assert (error.cause, error.state_cause) == (ADMIN_APPROVAL_REQUIRED, ADMIN_APPROVAL_REQUIRED)
    assert error.message == "Your organization needs to approve CORSIA before it can connect this account."
    assert MicrosoftError(INPUT_REFUSAL).state_cause is None and MicrosoftError(INTERNAL).state_cause is None


async def test_identity_comes_from_graph_me_with_the_access_token(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)

    [row] = _accounts()
    assert row["id"] == account_id
    assert (row["provider_subject"], row["address"], row["account_kind"]) == (f"{TENANT_A}:object-a", "same@example.test", "work")
    assert microsoft.graph_auth == [f"Bearer {ACCESS}"]
    assert _bundle(account_id) == {
        "access_token": ACCESS, "refresh_token": REFRESH, "client_id": CLIENT_ID, "tenant_id": TENANT_A,
        "scopes": list(oauth.SCOPES),
    }


async def test_graph_falls_back_to_the_user_principal_name(microsoft, isolated_db) -> None:
    microsoft.me = {"id": "object-a", "mail": None, "userPrincipalName": "upn@example.test"}
    await _connect(microsoft)
    assert [row["address"] for row in _accounts()] == ["upn@example.test"]


async def test_both_reads_the_organization_to_tell_work_from_personal(microsoft, isolated_db) -> None:
    microsoft.organizations = [{"id": TENANT_B}]
    await _connect(microsoft, kind="both", tenant=None)
    [row] = _accounts()
    assert (row["account_kind"], row["provider_subject"]) == ("work", f"{TENANT_B}:object-a")


async def test_both_treats_only_graphs_msa_organization_response_as_personal(microsoft, isolated_db) -> None:
    microsoft.organization_status = 400
    microsoft.organization_error = {"error": {"code": "BadRequest", "message": "not supported for MSA accounts"}}

    await _connect(microsoft, kind="both", tenant=None)

    [row] = _accounts()
    assert (row["account_kind"], row["provider_subject"]) == ("personal", "9188040d-6c67-4c5b-b112-36a304b66dad:object-a")


@pytest.mark.parametrize("status", (401, 403, 404, 500))
async def test_both_rejects_other_organization_failures_without_persisting_an_account(microsoft, isolated_db, status) -> None:
    microsoft.organization_status = status
    url, _ = await oauth.start_sign_in(CLIENT_ID, "both", actor=("system", "test"), reach=EVERYWHERE)

    result, page = await _finish(url)

    assert result == 400
    assert _page_has(page, MicrosoftError(PROVIDER_UNAVAILABLE).browser_message)
    assert _accounts() == []


async def test_microsoft_lifecycle_events_are_emitted_once(microsoft, isolated_db, monkeypatch) -> None:
    from connections import account_state

    events: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        account_state, "emit_product_event",
        lambda category, action, **kwargs: events.append((category, action, kwargs["properties"])),
    )
    account_id = await _connect(microsoft)
    microsoft.token_error = True
    with pytest.raises(MicrosoftError, match=REFRESH_TOKEN_INVALID):
        await client.access_token(account_id)
    await account_actions.sign_out_account(account_id, actor=PERSON)
    await account_actions.remove_account(account_id, actor=PERSON)
    failed_url, _ = await oauth.start_sign_in(CLIENT_ID, "both", actor=("system", "test"), reach=EVERYWHERE)
    failed_status, _ = await _finish(failed_url, error="access_denied", error_subcode="cancel")

    assert failed_status == 400
    assert [(category, action) for category, action, _ in events] == [
        ("connected_account", "connect_started"),
        ("connected_account", "connect_succeeded"),
        ("connected_account", "connect_failed"),
        ("connected_account", "signed_out"),
        ("connected_account", "removed"),
        ("connected_account", "connect_started"),
        ("connected_account", "connect_failed"),
    ]
    assert all(event["provider"] == MICROSOFT_PROVIDER for _, _, event in events)
    assert events[2][2]["cause"] == REFRESH_TOKEN_INVALID
    assert events[-1][2]["cause"] == CONSENT_CANCELLED


async def test_a_graph_failure_adds_no_row_and_echoes_nothing(microsoft, isolated_db) -> None:
    microsoft.me_status = 401
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    status, page = await _finish(url)
    assert status == 400
    assert _page_has(page, MicrosoftError(PROVIDER_UNAVAILABLE).browser_message)
    assert b"graph-canary" not in page
    assert _accounts() == []


async def test_loopback_callback_uses_its_own_port_and_refuses_wrong_state(microsoft, isolated_db) -> None:
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    query = _query(url)
    redirect_uri, state = query["redirect_uri"], query["state"]
    pending = oauth._pending[state]
    assert redirect_uri == f"http://localhost:{pending.listener.port}"
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
    assert microsoft.forms[-1]["redirect_uri"] == redirect_uri


@pytest.mark.parametrize(("redirect_query", "cause"), [
    ({"error": "access_denied", "error_subcode": "cancel", "error_description": "provider-cancel-canary"}, CONSENT_CANCELLED),
    ({"error": "access_denied", "error_description": "AADSTS90094 provider-admin-canary"}, ADMIN_APPROVAL_REQUIRED),
    ({"error": "provider-unknown-canary", "error_description": "AADSTS99999 provider-unknown-canary"}, PROVIDER_UNAVAILABLE),
])
async def test_redirect_failures_are_redacted_and_observable_for_add_and_sign_in_again(
    microsoft, isolated_db, agent_adds, redirect_query, cause,
) -> None:
    add_url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("agent", "thread-a"), adding_thread_id="thread-a")
    add_status, add_page = await _finish(add_url, **redirect_query)
    assert add_status == 400
    assert oauth.accounts_status("thread-a") == {"status": "not_connected", "cause": cause, "message": message(cause)}
    assert _page_has(add_page, MicrosoftError(cause).browser_message)
    canaries = [value.encode() for value in redirect_query.values() if "canary" in value]
    assert canaries and not any(canary in add_page for canary in canaries)

    account_id = await _connect(microsoft)
    # A row whose sign-in still works keeps it when signing in again fails.
    kept_url, _ = await oauth.start_sign_in(None, None, actor=PERSON, account_id=account_id)
    assert (await _finish(kept_url, **redirect_query))[0] == 400
    assert oauth.accounts_status(account_id=account_id)["state"] == "connected"

    await account_actions.sign_out_account(account_id, actor=PERSON)
    again_url, _ = await oauth.start_sign_in(None, None, actor=PERSON, account_id=account_id)
    again_status, again_page = await _finish(again_url, **redirect_query)
    assert again_status == 400
    # Otherwise it needs sign-in, showing why.
    status = oauth.accounts_status(account_id=account_id)
    assert (status["state"], status["cause"]) == ("needs_sign_in", cause)
    assert not any(canary in again_page for canary in canaries)


@pytest.mark.parametrize(("payload", "cause"), [
    ({"error": "invalid_client", "error_codes": [65004]}, CONSENT_CANCELLED),
    ({"error": "unauthorized_client", "error_codes": [90094]}, ADMIN_APPROVAL_REQUIRED),
    ({"error": "access_denied", "error_codes": [90095, 65001]}, ADMIN_APPROVAL_REQUIRED),
    ({"error": "invalid_client"}, REGISTRATION_MISCONFIGURED),
    ({"error": "unauthorized_client"}, REGISTRATION_MISCONFIGURED),
    ({"error": "other", "error_codes": [50011]}, REGISTRATION_MISCONFIGURED),
    ({"error": "other", "error_codes": [700016]}, REGISTRATION_MISCONFIGURED),
    ({"error": "other", "error_codes": [7000218]}, REGISTRATION_MISCONFIGURED),
    ({"error": "invalid_grant", "error_codes": [99999]}, PROVIDER_UNAVAILABLE),
    ({"error": "invalid_grant", "error_codes": ["90094"]}, PROVIDER_UNAVAILABLE),
    (None, PROVIDER_UNAVAILABLE),
])
def test_code_exchange_errors_use_documented_registration_mapping(payload, cause) -> None:
    assert exchange_error_cause(payload) == cause


@pytest.mark.parametrize("query", [
    {"error": "invalid_client", "error_description": "provider-detail-canary"},
    {"error": "unauthorized_client", "error_description": "provider-detail-canary"},
    {"error": "access_denied", "error_description": "AADSTS50011 provider-detail-canary"},
    {"error": "access_denied", "error_description": "AADSTS700016 provider-detail-canary"},
    {"error": "access_denied", "error_description": "AADSTS7000218 provider-detail-canary"},
])
def test_redirect_registration_errors_are_fixed_and_redacted(query) -> None:
    error = oauth._redirect_error(query)
    assert error.cause == REGISTRATION_MISCONFIGURED
    assert "provider-detail-canary" not in error.message
    assert "provider-detail-canary" not in error.browser_message


async def test_registration_error_is_a_latest_add_result_and_a_row_state_cause(microsoft, isolated_db, agent_adds) -> None:
    add_url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("agent", "thread-a"), adding_thread_id="thread-a")
    add_status, add_page = await _finish(add_url, error="invalid_client", error_description="AADSTS50011 provider-detail-canary")
    assert add_status == 400
    assert oauth.accounts_status("thread-a") == {
        "status": "not_connected", "cause": REGISTRATION_MISCONFIGURED, "message": message(REGISTRATION_MISCONFIGURED),
    }
    assert _page_has(add_page, MicrosoftError(REGISTRATION_MISCONFIGURED).browser_message)
    assert b"provider-detail-canary" not in add_page

    account_id = await _connect(microsoft)
    await account_actions.sign_out_account(account_id, actor=PERSON)
    again_url, _ = await oauth.start_sign_in(None, None, actor=PERSON, account_id=account_id)
    again_status, again_page = await _finish(again_url, error="invalid_client", error_description="AADSTS50011 provider-detail-canary")
    assert again_status == 400
    status = oauth.accounts_status(account_id=account_id)
    assert (status["state"], status["cause"]) == ("needs_sign_in", REGISTRATION_MISCONFIGURED)
    assert _page_has(again_page, MicrosoftError(REGISTRATION_MISCONFIGURED).browser_message)
    assert b"provider-detail-canary" not in again_page


def test_registration_error_is_a_state_cause_with_fixed_copy() -> None:
    error = MicrosoftError(REGISTRATION_MISCONFIGURED)
    assert error.state_cause == REGISTRATION_MISCONFIGURED
    assert error.message.startswith("The person's own Microsoft app registration isn't set up right.")
    assert error.browser_message == "Your Microsoft app for CORSIA isn't set up right. Go back to CORSIA and ask it to fix the app."
    assert "provider-detail-canary" not in error.message
    assert "provider-detail-canary" not in error.browser_message


async def test_success_clears_latest_add_failure(microsoft, isolated_db, agent_adds) -> None:
    failed_url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("agent", "thread-a"), adding_thread_id="thread-a")
    await _finish(failed_url, error="access_denied", error_subcode="cancel")
    assert oauth.accounts_status("thread-a")["cause"] == CONSENT_CANCELLED

    success_url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("agent", "thread-a"), adding_thread_id="thread-a")
    status, _ = await _finish(success_url)
    assert status == 200
    assert [row["address"] for row in _accounts()] == ["same@example.test"]
    with oauth._pending_lock:
        assert "thread-a" not in oauth._latest_add_failures


async def test_add_failure_status_expires_with_pending_lifetime(microsoft, isolated_db, agent_adds, monkeypatch) -> None:
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0.01)
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("agent", "expiry-thread"), adding_thread_id="expiry-thread")
    listener = oauth._pending[_query(url)["state"]].listener

    status, _ = await _finish(url, error="access_denied", error_subcode="cancel")
    assert status == 400
    assert oauth.accounts_status("expiry-thread")["cause"] == CONSENT_CANCELLED
    assert not oauth._pending
    assert listener.bound_addresses() == []

    await asyncio.sleep(0.05)

    assert oauth.accounts_status("expiry-thread") == {"status": "not_connected"}


async def test_code_exchange_failure_page_never_echoes_code_or_provider_detail(microsoft, isolated_db) -> None:
    microsoft.token_error = True
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    status, page = await _finish(url, code="authorization-code-canary")
    assert status == 400
    assert _page_has(page, MicrosoftError(PROVIDER_UNAVAILABLE).browser_message)
    assert b"authorization-code-canary" not in page
    assert b"token-error-canary" not in page
    assert not oauth._pending


async def test_a_fault_inside_corsia_is_internal_and_logs_no_detail(microsoft, isolated_db, monkeypatch, caplog) -> None:
    def broken(*_args):
        raise RuntimeError("same@example.test detail-canary")

    monkeypatch.setattr(account_store, "store", broken)
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    status, page = await _finish(url)
    assert status == 500
    assert _page_has(page, MicrosoftError(INTERNAL).browser_message)
    assert b"detail-canary" not in page
    assert "detail-canary" not in caplog.text and "RuntimeError" in caplog.text
    assert oauth.accounts_status() == {"status": "not_connected", "cause": INTERNAL, "message": message(INTERNAL)}


async def test_timeout_closes_pending_loopback_listener(microsoft, isolated_db, monkeypatch) -> None:
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0.01)
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    listener = oauth._pending[_query(url)["state"]].listener
    await asyncio.sleep(0.05)
    assert not oauth._pending
    assert listener.bound_addresses() == []


async def test_shutdown_closes_pending_loopback_listener(microsoft, isolated_db) -> None:
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    listener = oauth._pending[_query(url)["state"]].listener
    await oauth.stop_all_signins()
    assert not oauth._pending
    assert listener.bound_addresses() == []


def test_microsoft_backend_has_no_fixed_callback_route() -> None:
    from api.microsoft import router

    assert "/oauth/callback" not in {route.path for route in router.routes}


async def test_same_address_in_two_tenants_has_two_rows(microsoft, isolated_db) -> None:
    first = await _connect(microsoft)
    microsoft.me = {"id": "object-b", "mail": "same@example.test"}
    second = await _connect(microsoft, tenant=TENANT_B)
    assert first != second
    rows = _accounts()
    assert {row["provider_subject"] for row in rows} == {f"{TENANT_A}:object-a", f"{TENANT_B}:object-b"}
    assert {row["address"] for row in rows} == {"same@example.test"}
    assert {row["account_kind"] for row in rows} == {"work"}


async def test_adding_an_account_that_is_already_there_says_so(microsoft, isolated_db) -> None:
    await _connect(microsoft)
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=("system", "test"), reach=EVERYWHERE)
    status, page = await _finish(url)
    assert status == 400
    assert _page_has(page, "same@example.test is already connected.")
    assert len(_accounts()) == 1


# Sign in again: ``start_sign_in(..., account_id=...)``.

async def test_sign_in_again_reuses_the_saved_registration_and_ignores_the_callers(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)
    await account_actions.sign_out_account(account_id, actor=PERSON)
    assert _bundle(account_id) is None

    url, returned_id = await oauth.start_sign_in(OTHER_CLIENT, "personal", TENANT_B, actor=PERSON, account_id=account_id)

    query = _query(url)
    assert returned_id == account_id
    assert query["client_id"] == CLIENT_ID
    assert url.startswith(f"https://login.microsoftonline.com/{TENANT_A}/")
    status, _ = await _finish(url)
    assert status == 200
    assert microsoft.forms[-1]["client_id"] == CLIENT_ID
    assert microsoft.token_urls[-1].startswith(f"https://login.microsoftonline.com/{TENANT_A}/")
    [row] = _accounts()
    assert (row["id"], row["state"], row["state_cause"]) == (account_id, "connected", None)
    assert _bundle(account_id)["client_id"] == CLIENT_ID


async def test_sign_in_again_as_someone_else_changes_nothing(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)
    microsoft.me = {"id": "object-z", "mail": "other@example.test"}
    url, _ = await oauth.start_sign_in(None, None, actor=PERSON, account_id=account_id)
    status, page = await _finish(url)
    assert status == 400
    assert _page_has(page, MicrosoftError(DIFFERENT_ACCOUNT, signed_in="other@example.test", address="same@example.test").browser_message)
    [row] = _accounts()
    assert (row["address"], row["provider_subject"]) == ("same@example.test", f"{TENANT_A}:object-a")


async def test_sign_in_again_refuses_an_unknown_invisible_or_google_account_alike(microsoft, isolated_db) -> None:
    hidden = await _connect(microsoft)
    make_only_corsia(hidden)
    google = create_account(GOOGLE_PROVIDER, "g@example.test", reach=EVERYWHERE, created_by=("system", "test"))
    exchanges = len(microsoft.forms)

    for account_id, thread_id in (("no-such-id", None), (hidden, "thread-a"), (str(google["id"]), None)):
        with pytest.raises(MicrosoftError) as refused:
            await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, account_id=account_id, adding_thread_id=thread_id)
        assert refused.value.cause == ACCOUNT_NOT_FOUND
    assert not oauth._pending and len(microsoft.forms) == exchanges


async def test_cancelling_sign_in_again_leaves_the_row_needing_sign_in(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)
    await account_actions.sign_out_account(account_id, actor=PERSON)
    url, _ = await oauth.start_sign_in(None, None, actor=PERSON, account_id=account_id)
    status, _ = await _finish(url, error="access_denied", error_subcode="cancel")
    assert status == 400
    [row] = _accounts()
    assert (row["state"], row["state_cause"]) == ("needs_sign_in", CONSENT_CANCELLED)
    assert _bundle(account_id) is None


async def test_remove_deletes_row_and_pending_without_session_revocation(microsoft, isolated_db) -> None:
    account_id = await _connect(microsoft)
    url, _ = await oauth.start_sign_in(None, None, account_id=account_id, actor=PERSON)
    listener = oauth._pending[_query(url)["state"]].listener
    result = await account_actions.remove_account(account_id, actor=PERSON)
    assert result["revocation"] == "manual_only"
    assert _accounts() == []
    assert not oauth._pending
    assert listener.bound_addresses() == []
    assert not any(form.get("token") for form in microsoft.forms)


async def test_status_contract(microsoft, isolated_db) -> None:
    assert oauth.accounts_status() == {"status": "not_connected"}
    account_id = await _connect(microsoft)
    one = oauth.accounts_status(account_id=account_id)
    assert one == {
        "account_id": account_id, "address": "same@example.test", "kind": "work", "state": "connected",
        "cause": None, "reach": EVERYWHERE.as_dict(),
    }


# The shared Connected apps rows, Microsoft and Google side by side.

async def test_rows_show_microsoft_and_google_side_by_side_with_registration_guids_only(microsoft, isolated_db) -> None:
    work = await _connect(microsoft)
    microsoft.me = {"id": "object-p", "mail": "personal@example.test"}
    personal = await _connect(microsoft, kind="personal", tenant=None)
    google = str(create_account(GOOGLE_PROVIDER, "g@example.test", reach=EVERYWHERE, created_by=("system", "test"))["id"])
    store_credentials(google, {"client_id": "google-client-canary", "client_secret": "google-secret-canary", "refresh_token": "google-refresh-canary"})
    selected = AppReach(mode="selected", project_ids=frozenset(), outside_projects=True)
    account_actions.change_account_reach(work, selected, actor=PERSON)
    await account_actions.sign_out_account(personal, actor=PERSON)

    db = get_db()
    try:
        rows = {row["id"]: row for row in list_account_rows(None, db=db)}
        keys = [str(account["credential_key"]) for account in list_accounts(db=db)]
    finally:
        db.close()

    assert rows[work] == {
        "kind": "account", "id": work, "provider": MICROSOFT_PROVIDER, "address": "same@example.test",
        "capabilities": ["mail", "calendar", "files"], "state": "connected", "reach": selected.as_dict(),
        "account_kind": "work", "cause": None, "client_id": CLIENT_ID, "tenant_id": TENANT_A,
    }
    assert {key: rows[personal][key] for key in ("provider", "state", "account_kind", "client_id", "tenant_id")} == {
        "provider": MICROSOFT_PROVIDER, "state": "signed_out", "account_kind": "personal", "client_id": CLIENT_ID, "tenant_id": None,
    }
    assert (rows[google]["provider"], rows[google]["client_id"], rows[google]["tenant_id"]) == (GOOGLE_PROVIDER, None, None)
    text = json.dumps(list(rows.values()))
    for secret in (ACCESS, REFRESH, "google-client-canary", "google-secret-canary", "google-refresh-canary", *keys):
        assert secret not in text


# Agent tools.

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
        "properties": {
            "account_id": {"type": "string", "description": "An existing account to sign in again; omit to add one."},
            "client_id": {"type": "string", "description": "Microsoft Application (client) ID GUID, required to add."},
            "kind": {"type": "string", "enum": ["personal", "work", "both"], "description": "Required to add."},
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


async def test_microsoft_connect_returns_the_fixed_message(monkeypatch) -> None:
    from tools import microsoft_account

    async def designated(_kwargs):
        return None

    monkeypatch.setattr(microsoft_account, "_designated", designated)
    result = await microsoft_account.MicrosoftConnectTool().execute(_thread_id="thread-a", client_id="not-a-guid", kind="work")
    assert result == f"Error: {INPUT_REFUSAL}: {message(INPUT_REFUSAL)}"


# Person-only Sign out and Remove share ``api.connected_account_rows`` with Google;
# that route's direct control coverage remains in test_connected_account_rows.py.
