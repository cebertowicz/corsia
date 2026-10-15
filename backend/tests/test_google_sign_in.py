"""The platform-held Google sign-in: the key-store prefix, the OAuth flow, and token hygiene.

Google is a ``MockTransport``; the canary refresh token and client secret must
appear in no tool result, error, HTTP response or log line.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN
from fastapi import FastAPI

from api import google as google_api
from connections.google import client as google_client
from connections.google import oauth
from infra import key_store
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from infra.settings import get_setting
from tests.registry_helpers import make_session
from tools.corsia_api import CorsiaApiTool
from tools.google_account import GoogleConnectTool, GoogleStatusTool

REFRESH = "1//canaryREFRESHtoken0123456789abcdef"
SECRET = "canary-client-secret-0123456789"
CLIENT_ID = "1234-canary.apps.googleusercontent.com"
ACCESS = "ya29.canaryAccessToken0123456789"
EMAIL = "person@example.com"
_REAL_CLIENT = httpx.AsyncClient


class FakeGoogle:
    """Google's token, profile and API endpoints; records each request body."""

    def __init__(self) -> None:
        self.challenge = ""
        self.token_forms: list[dict] = []
        self.refresh_status = 200
        self.scope = " ".join(oauth.SCOPES)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == oauth.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            if form["grant_type"] == "refresh_token":
                if self.refresh_status != 200:
                    return httpx.Response(self.refresh_status, json={"error": "invalid_grant", "echo": REFRESH})
                return httpx.Response(200, json={"access_token": ACCESS})
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": ACCESS, "refresh_token": REFRESH, "scope": self.scope,
            })
        if url == oauth.PROFILE_URL:
            return httpx.Response(200, json={"emailAddress": EMAIL})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def google(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    fake = FakeGoogle()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(google_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(oauth, "_pending", None)
    return fake


@pytest.fixture
def conductor(google):
    db = get_db()
    thread_id = make_session(db, prefix="conductor")
    assert set_corsia_agent(thread_id, db=db)
    return thread_id


@pytest.fixture
def client_file(tmp_path):
    path = tmp_path / "client_secret_test.json"
    path.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": SECRET}}))
    return str(path)


def _http(*, client=("127.0.0.1", 50000)) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(google_api.router)
    return _REAL_CLIENT(transport=httpx.ASGITransport(app=app, client=client), base_url="http://test")


async def _start(google: FakeGoogle, conductor: str, client_file: str) -> tuple[str, str]:
    out = await GoogleConnectTool().execute(_thread_id=conductor, client_file=client_file)
    url = json.loads(out)["url"]
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]
    return url, query["state"][0]


async def test_post_api_keys_refuses_google_names(conductor):
    for name in key_store.GOOGLE_ENTRIES + ("google-anything",):
        out = await CorsiaApiTool().execute(
            "POST", "/api/keys", body={"name": name, "label": "x"}, _thread_id=conductor,
        )
        assert json.loads(out)["status"] >= 400, out
    assert key_store.resolve_google() is None


async def test_connect_builds_a_pkce_loopback_url_and_keeps_the_secret_out(google, conductor, client_file):
    url, state = await _start(google, conductor, client_file)
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert query["redirect_uri"][0].endswith("/api/google/oauth/callback")
    assert set(query["scope"][0].split()) == set(oauth.SCOPES)
    assert query["access_type"] == ["offline"]
    assert SECRET not in url
    # Nothing is saved until Google answers.
    assert key_store.resolve_google() is None
    assert state


async def test_callback_completes_the_sign_in_and_stores_all_three_entries(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200
    assert REFRESH not in response.text and SECRET not in response.text
    assert key_store.resolve_google() == {"client_id": CLIENT_ID, "client_secret": SECRET, "refresh_token": REFRESH}
    exchange = google.token_forms[0]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "auth-code"
    account = get_setting("google_account")
    assert account["email"] == EMAIL and account["status"] == "connected"
    assert set(account["scopes"]) == set(oauth.SCOPES)
    # The settings table carries no value.
    assert REFRESH not in json.dumps(account) and SECRET not in json.dumps(account)


async def test_callback_with_the_wrong_state_is_refused_and_single_use(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        wrong = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": "forged"})
        assert wrong.status_code == 400
        assert google.token_forms == []
        # A forged callback must not cancel the person's sign-in in progress.
        right = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
        assert right.status_code == 200
        # The right state is single-use.
        again = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
        assert again.status_code == 400
    assert len(google.token_forms) == 1
    assert key_store.resolve_google() is not None


async def test_callback_with_a_mismatched_pkce_verifier_is_refused(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    google.challenge = "a-different-challenge"
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 400
    assert key_store.resolve_google() is None


async def test_callback_is_loopback_only(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    params = {"code": "auth-code", "state": state}
    async with _http() as http:
        proxied = await http.get(
            "/api/google/oauth/callback", params=params, headers={"tailscale-user-login": "a@b.example"},
        )
    async with _http(client=("100.64.0.9", 50000)) as http:
        remote = await http.get("/api/google/oauth/callback", params=params)
    assert proxied.status_code == 403 and remote.status_code == 403
    assert google.token_forms == []


async def test_refused_when_google_withholds_a_scope(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    google.scope = oauth.SCOPES[0]
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 400
    assert key_store.resolve_google() is None


async def test_not_connected_text_and_status_shape(google, conductor):
    with pytest.raises(google_client.GoogleNotConnected) as caught:
        await google_client.get_json("https://www.googleapis.com/calendar/v3/users/me/calendarList")
    assert str(caught.value) == "Google isn't connected. Call google_connect and give the person the link."
    status = json.loads(await GoogleStatusTool().execute(_thread_id=conductor))
    assert set(status) == {"email", "scopes", "status", "poller_state", "last_success_at"}
    assert status["status"] == "not_connected" and status["email"] is None


async def test_connect_when_connected_and_without_a_client_file(google, conductor, client_file):
    out = await GoogleConnectTool().execute(_thread_id=conductor)
    assert out.startswith("Error: Google needs the person's OAuth client file")
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    out = await GoogleConnectTool().execute(_thread_id=conductor)
    assert out == f"Error: Google is already connected as {EMAIL}."
    status = json.loads(await GoogleStatusTool().execute(_thread_id=conductor))
    assert status["status"] == "connected" and status["email"] == EMAIL


async def test_a_file_that_is_not_a_client_is_refused_without_echo(google, conductor, tmp_path):
    bad = tmp_path / "other.json"
    bad.write_text(json.dumps({"web": {"client_secret": SECRET}}))
    out = await GoogleConnectTool().execute(_thread_id=conductor, client_file=str(bad))
    assert out.startswith("Error: That file isn't a Google Desktop app client file")
    assert SECRET not in out


async def test_only_the_designated_thread_may_use_the_tools(google, client_file):
    other = make_session(get_db(), prefix="other")
    for tool in (GoogleConnectTool(), GoogleStatusTool()):
        out = await tool.execute(_thread_id=other, client_file=client_file)
        assert out == "Error: Only the designated CORSIA thread can use Google tools."
    assert (await GoogleStatusTool().execute()).startswith("Error:")


async def test_no_token_or_secret_reaches_any_output_or_log(google, conductor, client_file, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    outputs: list[str] = []
    url, state = await _start(google, conductor, client_file)
    outputs.append(url)
    async with _http() as http:
        callback = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
        outputs.append(callback.text)
    outputs.append(await GoogleStatusTool().execute(_thread_id=conductor))
    outputs.append(await GoogleConnectTool().execute(_thread_id=conductor))
    # A refresh, then a refresh Google rejects (its body echoes the token).
    outputs.append(json.dumps(await google_client.get_json("https://gmail.googleapis.com/gmail/v1/users/me/labels")))
    google.refresh_status = 400
    with pytest.raises(google_client.GoogleSignInRejected) as rejected:
        await google_client.get_json("https://gmail.googleapis.com/gmail/v1/users/me/labels")
    outputs.append(str(rejected.value))
    outputs.append(await GoogleStatusTool().execute(_thread_id=conductor))
    assert json.loads(outputs[-1])["status"] == "needs_sign_in"
    captured = capsys.readouterr()
    outputs.extend([caplog.text, captured.out, captured.err])
    for text in outputs:
        for canary in (REFRESH, SECRET, ACCESS):
            assert canary not in text
    # The saved client survives a rejected token, so reconnecting needs no client file.
    assert (await GoogleConnectTool().execute(_thread_id=conductor)).startswith("{")


async def test_routes_need_a_signed_in_caller(google):
    async with _http() as http:
        assert (await http.get("/api/google/status")).status_code == 401
        ok = await http.get("/api/google/status", headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"})
    assert ok.status_code == 200 and ok.json()["status"] == "not_connected"
