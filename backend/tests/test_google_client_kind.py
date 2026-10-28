"""Google client selection and recovery use one saved discriminator."""

import httpx
import pytest

from connections import connected_accounts
from connections.google import client, oauth, provider


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False)])
def test_client_selection_and_required_fields(kind, published):
    bundle = {"client_id": "id", "refresh_token": "refresh"}
    if kind is not None:
        bundle["client"] = kind
    if not published:
        bundle["client_secret"] = "secret"
    assert client.uses_published(bundle) is published
    assert provider.GOOGLE.valid_bundle(bundle)
    assert not provider.GOOGLE.valid_bundle({k: v for k, v in bundle.items() if k != "refresh_token"})


def test_unknown_client_is_invalid():
    bundle = {"client": "unknown", "client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
    with pytest.raises(ValueError):
        client.uses_published(bundle)
    assert not provider.GOOGLE.valid_bundle(bundle)


@pytest.mark.parametrize("kind,expected", [("published", None), ("own", ("id", "secret")), (None, ("id", "secret")), ("unknown", "invalid")])
def test_reconnect_selects_saved_client(kind, expected, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "secret"}
    if kind is not None:
        bundle["client"] = kind
    monkeypatch.setattr(connected_accounts, "_stored_bundle", lambda *args, **kwargs: (provider.GOOGLE, bundle))
    monkeypatch.setattr(connected_accounts, "account_client", lambda *args, **kwargs: bundle)
    if expected == "invalid":
        with pytest.raises(client.GoogleSignInError, match="invalid"):
            oauth._client_for("account", None, None)
    else:
        assert oauth._client_for("account", None, None) == expected


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False)])
def test_saved_recovery_uses_client(kind, published, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
    if kind is not None:
        bundle["client"] = kind
    monkeypatch.setattr(connected_accounts, "_stored_bundle", lambda *args, **kwargs: (provider.GOOGLE, bundle))
    expected = client.PUBLISHED_SIGN_IN_RECOVERY if published else client.SIGN_IN_RECOVERY
    assert client.sign_in_recovery("account", db=None) == expected


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False), ("unknown", None)])
def test_saved_account_lookup(kind, published, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
    if kind is not None:
        bundle["client"] = kind
    monkeypatch.setattr(connected_accounts, "resolve_credentials", lambda *args, **kwargs: bundle if provider.GOOGLE.valid_bundle(bundle) else None)
    if published is None:
        with pytest.raises(client.GoogleNotConnected):
            client._published_account("account", db=None)
    else:
        assert client._published_account("account", db=None) is published


def test_unknown_saved_client_refuses_recovery(monkeypatch):
    bundle = {"client": "unknown", "client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
    monkeypatch.setattr(connected_accounts, "_stored_bundle", lambda *args, **kwargs: (provider.GOOGLE, bundle))
    with pytest.raises(ValueError):
        client.sign_in_recovery("account", db=None)


@pytest.mark.parametrize("kind,published", [("published", True), ("own", False), (None, False)])
@pytest.mark.parametrize("status", [200, 400, 401])
async def test_refresh_selects_endpoint_and_recovery(kind, published, status, monkeypatch):
    bundle = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
    if kind is not None:
        bundle["client"] = kind
    requests = []
    async def run(_runner, operation, *args):
        return bundle if operation is client._usable_credentials else None
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        def handler(request):
            requests.append(request)
            return httpx.Response(status, json={"access_token": "access"})
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(client, "run_in_db", run)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    if status == 200:
        assert await client.access_token("account") == "access"
    else:
        with pytest.raises(client.GoogleSignInRejected) as error:
            await client.access_token("account")
        assert str(error.value) == (client.PUBLISHED_SIGN_IN_RECOVERY if published else client.SIGN_IN_RECOVERY)
    assert str(requests[0].url) == (client.PUBLISHED_REFRESH_URL if published else client.TOKEN_URL)


@pytest.mark.parametrize("published", [True, False])
@pytest.mark.parametrize("supplied", [True, False])
async def test_api_401_recovery_uses_client_once(published, supplied, monkeypatch):
    lookups = []
    async def acquire(_account):
        return "access", published
    async def run(_runner, operation, *args):
        if operation is client._published_account:
            lookups.append(args[0])
            return published
        return None
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kwargs)
    monkeypatch.setattr(client, "_access_token", acquire)
    monkeypatch.setattr(client, "run_in_db", run)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    with pytest.raises(client.GoogleSignInRejected) as error:
        await client.request_json("GET", "https://example.test", account_id="account", token="access" if supplied else None)
    assert str(error.value) == (client.PUBLISHED_SIGN_IN_RECOVERY if published else client.SIGN_IN_RECOVERY)
    assert lookups == (["account"] if supplied else [])
