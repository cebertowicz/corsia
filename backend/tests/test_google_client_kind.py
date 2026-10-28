"""Google client selection and recovery use one saved discriminator."""

import httpx
import pytest

from connections import connected_accounts
from connections.google import client, oauth, provider


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False)])
def test_client_selection_and_required_fields(kind, published):
    bundle = {"client_id": "id", "refresh_token": "fixture-refresh"}
    if kind is not None:
        bundle["client"] = kind
    if not published:
        bundle["client_secret"] = "fixture-secret"
    assert client.uses_published(bundle) is published
    assert provider.GOOGLE.valid_bundle(bundle)
    assert not provider.GOOGLE.valid_bundle({k: v for k, v in bundle.items() if k != "refresh_token"})
    if not published:
        assert not provider.GOOGLE.valid_bundle({k: v for k, v in bundle.items() if k != "client_secret"})


def test_only_the_published_marker_selects_the_published_client():
    assert not client.uses_published({"client": "older-marker"})
    assert not client.uses_published({"client": None})


@pytest.mark.parametrize("kind,expected", [("published", None), ("own", ("id", "fixture-secret")), (None, ("id", "fixture-secret"))])
def test_reconnect_selects_saved_client(kind, expected, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "fixture-secret"}
    if kind is not None:
        bundle["client"] = kind
    monkeypatch.setattr(connected_accounts, "account_client", lambda *args, **kwargs: bundle)
    assert oauth._client_for("account", None, None) == expected


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False)])
def test_saved_recovery_uses_client(kind, published, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "fixture-secret", "refresh_token": "fixture-refresh"}
    if kind is not None:
        bundle["client"] = kind
    monkeypatch.setattr(connected_accounts, "account_client", lambda *args, **kwargs: bundle)
    expected = client.PUBLISHED_SIGN_IN_RECOVERY if published else client.SIGN_IN_RECOVERY
    assert client.sign_in_recovery("account", db=None) == expected


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False)])
@pytest.mark.parametrize("status", [200, 400, 401])
async def test_refresh_selects_endpoint_and_recovery(kind, published, status, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "fixture-secret", "refresh_token": "fixture-refresh"}
    if kind is not None:
        bundle["client"] = kind
    requests = []
    async def run(_runner, operation, *args):
        return bundle if operation is client._usable_credentials else None
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        def handler(request):
            requests.append(request)
            return httpx.Response(status, json={"access_token": "fixture-access"})
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(client, "run_in_db", run)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    if status == 200:
        assert await client.access_token("account") == "fixture-access"
    else:
        with pytest.raises(client.GoogleSignInRejected) as error:
            await client.access_token("account")
        assert str(error.value) == (client.PUBLISHED_SIGN_IN_RECOVERY if published else client.SIGN_IN_RECOVERY)
    assert str(requests[0].url) == (client.PUBLISHED_REFRESH_URL if published else client.TOKEN_URL)


@pytest.mark.parametrize("published", [True, False])
@pytest.mark.parametrize("supplied", [True, False])
async def test_api_401_recovery_uses_client_without_another_read(published, supplied, monkeypatch):
    operations = []
    async def acquire(_account):
        return "fixture-access", published
    async def run(_runner, operation, *args):
        operations.append(operation.__name__)
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kwargs)
    monkeypatch.setattr(client, "access_grant", acquire)
    monkeypatch.setattr(client, "run_in_db", run)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    with pytest.raises(client.GoogleSignInRejected) as error:
        await client.request_json("GET", "https://example.test", account_id="account",
                                  token="fixture-access" if supplied else None, published=published)
    assert str(error.value) == (client.PUBLISHED_SIGN_IN_RECOVERY if published else client.SIGN_IN_RECOVERY)
    assert operations == ["mark_needs_sign_in"]


@pytest.mark.parametrize("status,body,error_type,marked", [
    (400, {"error": "invalid_grant"}, client.GoogleSignInRejected, True),
    (500, "An error occurred during token refresh.", client.GoogleRequestFailed, False),
])
async def test_published_refresh_preserves_google_error_semantics(status, body, error_type, marked, monkeypatch):
    # Google's workspace/cloud_function/index.js:327-336 passes Google's status/body
    # through on a Google error; its own faults return 500, not invalid_grant.
    bundle = {"client": "published", "client_id": "id", "refresh_token": "fixture-refresh"}
    operations = []
    async def run(_runner, operation, *args):
        operations.append(operation.__name__)
        return bundle if operation is client._usable_credentials else None
    real_client = httpx.AsyncClient
    def handler(request):
        assert str(request.url) == client.PUBLISHED_REFRESH_URL
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
    monkeypatch.setattr(client, "run_in_db", run)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(error_type) as error:
        await client.access_token("account")
    assert ("mark_needs_sign_in" in operations) is marked
    if marked:
        assert str(error.value) == client.PUBLISHED_SIGN_IN_RECOVERY
    else:
        assert error.value.status == 500


def test_status_keeps_healthy_accounts_when_one_saved_row_is_bad(isolated_db):
    from connections.app_reach import AppReach
    from infra import key_store
    import json

    from infra.database import get_db

    db = get_db()
    for account_id in ("healthy", "bad"):
        connected_accounts.create_account("google", account_id + "@example.test", reach=AppReach("all", frozenset(), True),
                                         created_by=("person", "owner"), account_id=account_id, db=db)
    connected_accounts.store_credentials("healthy", {
        "client": "published", "client_id": "id", "refresh_token": "fixture-refresh",
    }, db=db)
    row = connected_accounts.get_account("bad", db=db)
    key_store.set_managed_value(row["credential_key"], json.dumps({"client": "older-marker", "client_id": "id"}, separators=(",", ":")), db=db)
    db.close()
    rows = {row["account_id"]: row for row in oauth.accounts_status()["accounts"]}
    assert rows["healthy"]["status"] == "connected"
    assert rows["bad"]["status"] == "needs_sign_in"
    assert rows["bad"]["needs_action"] == client.SIGN_IN_RECOVERY
