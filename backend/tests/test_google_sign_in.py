"""The platform-held Google sign-in: the key-store prefix, the OAuth flow, and token hygiene.

Google is a ``MockTransport``; the canary refresh token and client secret must
appear in no tool result, error, HTTP response or log line.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN
from fastapi import FastAPI

from api import google as google_api
from connections.connected_accounts import google_accounts, resolve_google_credentials
from connections.google import client as google_client
from connections.google import oauth
from infra import key_store
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from infra.db_async import run_in_db
from infra.internal_principal import internal_request
from tests.registry_helpers import make_session
from tools.corsia_api import CorsiaApiTool
from tools.google_account import GoogleConnectTool, GoogleStatusTool

REFRESH = "1//canaryREFRESHtoken0123456789abcdef"
SECRET = "canary-client-secret-0123456789"
CLIENT_ID = "1234-canary.apps.googleusercontent.com"
ACCESS = "ya29.canaryAccessToken0123456789"
EMAIL = "person@example.com"
_REAL_CLIENT = httpx.AsyncClient


def _id_token(subject: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeGoogle:
    """Google's token, profile and API endpoints; records each request body."""

    def __init__(self) -> None:
        self.challenge = ""
        self.token_forms: list[dict] = []
        self.refresh_status = 200
        self.scope = " ".join(oauth.SCOPES)
        self.id_token: str | None = None
        self.email = EMAIL

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == oauth.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            if form["grant_type"] == "refresh_token":
                if self.refresh_status != 200:
                    return httpx.Response(self.refresh_status, json={"error": "invalid_grant", "echo": REFRESH})
                grant = {"access_token": ACCESS}
                if self.id_token is not None:
                    grant["id_token"] = self.id_token
                return httpx.Response(200, json=grant)
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            grant = {"access_token": ACCESS, "refresh_token": REFRESH, "scope": self.scope}
            if self.id_token is not None:
                grant["id_token"] = self.id_token
            return httpx.Response(200, json=grant)
        if url == oauth.PROFILE_URL:
            return httpx.Response(200, json={"emailAddress": self.email})
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
    monkeypatch.setattr(oauth, "_pending", {})
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


async def _start(google: FakeGoogle, conductor: str, client_file: str | None, **kwargs) -> tuple[str, str]:
    out = await GoogleConnectTool().execute(_thread_id=conductor, client_file=client_file, **kwargs)
    url = json.loads(out)["url"]
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]
    return url, query["state"][0]


async def _connect(google: FakeGoogle, conductor: str, client_file: str | None) -> str:
    """Add one account through the real flow; returns its account_id."""
    before = {account["id"] for account in _accounts()}
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200, response.text
    [added] = [account["id"] for account in _accounts() if account["id"] not in before]
    return str(added)


def _accounts() -> list[dict]:
    db = get_db()
    try:
        return google_accounts(db=db)
    finally:
        db.close()


def _only_account() -> dict | None:
    accounts = _accounts()
    assert len(accounts) <= 1, accounts
    return accounts[0] if accounts else None


def _credentials() -> dict | None:
    """The one account's usable bundle, or None when there is no account or no usable credential."""
    account = _only_account()
    if account is None:
        return None
    db = get_db()
    try:
        return resolve_google_credentials(str(account["id"]), db=db)
    finally:
        db.close()


async def test_post_api_keys_refuses_google_names(conductor):
    for name in key_store.GOOGLE_ENTRIES + ("google-anything",):
        out = await CorsiaApiTool().execute(
            "POST", "/api/keys", body={"name": name, "label": "x"}, _thread_id=conductor,
        )
        assert json.loads(out)["status"] >= 400, out
    assert _credentials() is None


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
    assert _credentials() is None
    assert state


async def test_callback_completes_the_sign_in_and_stores_all_three_entries(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200
    assert REFRESH not in response.text and SECRET not in response.text
    assert _credentials() == {"client_id": CLIENT_ID, "client_secret": SECRET, "refresh_token": REFRESH}
    exchange = google.token_forms[0]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "auth-code"
    account = _only_account()
    assert account is not None
    assert account["address"] == EMAIL and account["state"] == "connected"
    # The row carries no credential value.
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
    assert _credentials() is not None


async def test_callback_with_a_mismatched_pkce_verifier_is_refused(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    google.challenge = "a-different-challenge"
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 400
    assert _credentials() is None


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
    assert _credentials() is None


async def test_connects_when_the_person_declines_drive_docs_and_sheets(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    google.scope = " ".join(oauth.REQUIRED_SCOPES)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200
    assert _credentials() is not None
    [status] = json.loads(await GoogleStatusTool().execute(_thread_id=conductor))["accounts"]
    assert set(status["scopes"]) == set(oauth.REQUIRED_SCOPES)


async def test_token_subject_is_saved_without_requesting_a_new_scope(google, conductor, client_file):
    google.id_token = _id_token("google-subject-1")
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200
    assert _only_account()["provider_subject"] == "google-subject-1"
    # openid is asked for so Google returns the subject; declining it never refuses a sign-in.
    assert "openid" in oauth.SCOPES and "openid" not in oauth.REQUIRED_SCOPES


async def test_refresh_backfills_a_migrated_account_subject_when_google_supplies_one(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200
    account_id = str(_only_account()["id"])
    assert _only_account()["provider_subject"] is None
    google.id_token = _id_token("google-subject-2")
    await google_client.get_json(account_id, "https://gmail.googleapis.com/gmail/v1/users/me/labels")
    assert _only_account()["provider_subject"] == "google-subject-2"


async def test_not_connected_text_and_status_shape(google, conductor):
    with pytest.raises(google_client.GoogleNotConnected) as caught:
        await google_client.get_json("no-such-account", "https://www.googleapis.com/calendar/v3/users/me/calendarList")
    assert str(caught.value) == "Google isn't connected. Call google_connect and give the person the link."
    assert json.loads(await GoogleStatusTool().execute(_thread_id=conductor)) == {"accounts": []}


async def test_connect_when_connected_and_without_a_client_file(google, conductor, client_file):
    out = await GoogleConnectTool().execute(_thread_id=conductor)
    assert out == f"Error: {oauth.NEEDS_CLIENT_FILE}"
    assert "If none exists yet, follow skill `google-setup`" in out
    assert "Never create a second project or client when one already exists." in out
    account_id = await _connect(google, conductor, client_file)
    out = json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))
    assert out == {
        "reply": f"{EMAIL} is already connected for all your projects. To change where it works, use Where it works… on its row in Connected apps.",
        "link": f"#connected-apps/{account_id}",
    }
    [status] = json.loads(await GoogleStatusTool().execute(_thread_id=conductor))["accounts"]
    assert status["status"] == "connected" and status["email"] == EMAIL
    assert status["account_id"] == account_id and "needs_action" not in status
    # Adding the same Google identity again stores nothing and says where to manage it.
    _, state = await _start(google, conductor, None)
    async with _http() as http:
        again = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert again.status_code == 400 and f"{EMAIL} is already connected." in again.text
    assert [account["id"] for account in _accounts()] == [account_id]


@pytest.mark.parametrize(("reach", "projects", "reach_phrase"), [
    ({"mode": "all", "project_ids": [], "outside_projects": False}, [], " for all your projects"),
    ({"mode": "selected", "project_ids": ["garden"], "outside_projects": False}, [("garden", "Garden plan")], " for Garden plan"),
    ({"mode": "selected", "project_ids": ["garden", "launch"], "outside_projects": False}, [("garden", "Garden plan"), ("launch", "Q4 launch")], ""),
])
async def test_existing_connected_account_returns_the_b_g1_reply_for_each_reach(
    google, conductor, client_file, reach, projects, reach_phrase,
):
    account_id = await _connect(google, conductor, client_file)
    db = get_db()
    try:
        db.executemany(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            [(project_id, name, "t", "t") for project_id, name in projects],
        )
        db.commit()
    finally:
        db.close()
    from connections.google.account_actions import change_google_account_reach

    await run_in_db(change_google_account_reach, account_id, reach, actor=("person", "person"))
    if reach["mode"] == "selected":
        db = get_db()
        try:
            db.execute("UPDATE threads SET project_id='garden' WHERE id=?", (conductor,))
            db.commit()
        finally:
            db.close()
    result = json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))

    assert result == {
        "reply": f"{EMAIL} is already connected{reach_phrase}. To change where it works, use Where it works… on its row in Connected apps.",
        "link": f"#connected-apps/{account_id}",
    }
    assert [account["id"] for account in _accounts()] == [account_id]


async def test_existing_connected_account_reply_names_when_the_calling_scope_is_not_covered(
    google, conductor, client_file,
):
    """B-G1 distinguishes the account's reach from the scope this conversation asks for."""
    account_id = await _connect(google, conductor, client_file)
    db = get_db()
    try:
        db.executemany(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            [("garden", "Garden plan", "t", "t"), ("launch", "Q4 launch", "t", "t")],
        )
        db.commit()
    finally:
        db.close()
    from connections.google.account_actions import change_google_account_reach

    workspace_reply = f"{EMAIL} is already connected for all your projects. To change where it works, use Where it works… on its row in Connected apps."
    needs_sign_in_reply = f"{EMAIL} is already connected, but it needs you to sign in again. Use Sign in on its row in Connected apps."
    assert json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))["reply"] == workspace_reply

    db = get_db()
    try:
        db.execute("UPDATE threads SET project_id='launch' WHERE id=?", (conductor,))
        db.commit()
    finally:
        db.close()
    assert json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))["reply"] == workspace_reply

    await run_in_db(
        change_google_account_reach,
        account_id,
        {"mode": "selected", "project_ids": ["garden"], "outside_projects": False},
        actor=("person", "person"),
    )
    assert json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))["reply"] == (
        f"{EMAIL} is connected, but not for Q4 launch. To add Q4 launch, use Where it works… on its row in Connected apps."
    )

    db = get_db()
    try:
        db.execute("UPDATE threads SET project_id=NULL WHERE id=?", (conductor,))
        db.commit()
    finally:
        db.close()
    all_projects_reply = (
        f"{EMAIL} is connected, but not for all your projects. To make it work for all of them, use Where it works… on its row in Connected apps."
    )
    assert json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))["reply"] == all_projects_reply

    _, state = await _start(google, conductor, None)
    async with _http() as http:
        duplicate = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert duplicate.status_code == 400 and all_projects_reply in duplicate.text

    db = get_db()
    try:
        db.execute("UPDATE threads SET project_id='launch' WHERE id=?", (conductor,))
        db.execute("UPDATE connected_accounts SET state='needs_sign_in' WHERE id=?", (account_id,))
        db.commit()
    finally:
        db.close()
    assert json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))["reply"] == needs_sign_in_reply


async def test_person_readding_connected_account_gets_already_connected_reply(google, conductor, client_file):
    """A person adding through Connected apps must not be sent through a needless re-sign-in."""
    await _connect(google, conductor, client_file)

    async with _http() as http:
        started = await http.post("/api/google/connect", headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"})
        assert started.status_code == 200
        query = parse_qs(urlparse(started.json()["url"]).query)
        google.challenge = query["code_challenge"][0]
        duplicate = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": query["state"][0]})

    assert duplicate.status_code == 400
    assert f"{EMAIL} is already connected." in duplicate.text
    assert "sign in again" not in duplicate.text


async def test_existing_needs_sign_in_account_returns_its_row_link_without_a_second_row(google, conductor, client_file):
    account_id = await _connect(google, conductor, client_file)
    google.refresh_status = 400
    with pytest.raises(google_client.GoogleSignInRejected):
        await google_client.get_json(account_id, "https://gmail.googleapis.com/gmail/v1/users/me/labels")

    result = json.loads(await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id))

    assert result == {
        "reply": f"{EMAIL} is already connected, but it needs you to sign in again. Use Sign in on its row in Connected apps.",
        "link": f"#connected-apps/{account_id}",
    }
    assert [account["id"] for account in _accounts()] == [account_id]
    url, signing_in = await oauth.start_sign_in(actor=("person", "person"), account_id=account_id)
    assert url.startswith(oauth.AUTH_URL) and signing_in == account_id


async def test_reconnect_wrong_identity_names_the_saved_address_in_browser_copy(google, conductor, client_file):
    account_id = await _connect(google, conductor, client_file)
    db = get_db()
    try:
        db.execute("UPDATE connected_accounts SET state='needs_sign_in' WHERE id=?", (account_id,))
        db.commit()
    finally:
        db.close()
    google.email = "wrong@example.test"
    url, _ = await oauth.start_sign_in(actor=("person", "person"), account_id=account_id)
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]
    state = query["state"][0]
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 400
    assert f"That's a different Google account. Go back to CORSIA and sign in again as {EMAIL}." in html.unescape(response.text)


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
    account_id = str(_only_account()["id"])
    outputs.append(json.dumps(await google_client.get_json(account_id, "https://gmail.googleapis.com/gmail/v1/users/me/labels")))
    google.refresh_status = 400
    with pytest.raises(google_client.GoogleSignInRejected) as rejected:
        await google_client.get_json(account_id, "https://gmail.googleapis.com/gmail/v1/users/me/labels")
    outputs.append(str(rejected.value))
    outputs.append(await GoogleStatusTool().execute(_thread_id=conductor))
    [status] = json.loads(outputs[-1])["accounts"]
    assert status["status"] == "needs_sign_in"
    assert status["needs_action"] == (
        'Google rejected the saved sign-in. Sign-ins for a project in Testing expire after 7 days: '
        'if the Audience page of the OAuth consent screen in Google Cloud shows Testing, ask the person '
        'to publish their app "In production", keeping the same project. Then call google_connect with '
        'this account_id and post its reply verbatim followed by its link as [Open it in Connected apps](<link>).'
    )
    captured = capsys.readouterr()
    outputs.extend([caplog.text, captured.out, captured.err])
    for text in outputs:
        for canary in (REFRESH, SECRET, ACCESS):
            assert canary not in text
    # The saved client survives a rejected token, so reconnecting needs no client file.
    assert (await GoogleConnectTool().execute(_thread_id=conductor, account_id=account_id)).startswith("{")


async def test_agent_routes_filter_status_and_refuse_an_unreachable_reconnect(google, conductor, client_file):
    account_id = await _connect(google, conductor, client_file)
    other = make_session(get_db(), prefix="other-agent")
    db = get_db()
    try:
        db.executemany(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            [("visible", "Visible", "t", "t"), ("hidden", "Hidden", "t", "t")],
        )
        db.execute("UPDATE threads SET project_id='visible' WHERE id=?", (conductor,))
        db.execute("UPDATE threads SET project_id='hidden' WHERE id=?", (other,))
        db.commit()
    finally:
        db.close()
    from connections.google.account_actions import change_google_account_reach
    await run_in_db(
        change_google_account_reach, account_id,
        {"mode": "selected", "project_ids": ["visible"], "outside_projects": False}, actor=("person", "person"),
    )
    async with _http() as http:
        with internal_request("steve", conductor):
            visible = await http.get("/api/google/status")
        with internal_request("steve", other):
            hidden = await http.get("/api/google/status")
        with internal_request("steve", other):
            reconnect = await http.post("/api/google/connect", json={"account_id": account_id})
    assert [account["account_id"] for account in visible.json()["accounts"]] == [account_id]
    assert hidden.status_code == 200 and hidden.json() == {"accounts": []}
    assert reconnect.status_code == 403


async def test_person_google_routes_remain_unfiltered(google, conductor, client_file):
    account_id = await _connect(google, conductor, client_file)
    async with _http() as http:
        status = await http.get("/api/google/status", headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"})
        reconnect = await http.post(
            "/api/google/connect", headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"}, json={"account_id": account_id},
        )
    assert [account["account_id"] for account in status.json()["accounts"]] == [account_id]
    assert reconnect.status_code == 400 and "already connected" in reconnect.text


def test_concurrent_begins_never_exceed_the_pending_limit(monkeypatch):
    monkeypatch.setattr(oauth, "_client_for", lambda *args, **kwargs: (CLIENT_ID, SECRET))
    with ThreadPoolExecutor(max_workers=oauth.PENDING_MAX + 4) as pool:
        outcomes = list(pool.map(
            lambda _: _begin_for_test(), range(oauth.PENDING_MAX + 4),
        ))
    assert len(oauth._pending) == oauth.PENDING_MAX
    assert outcomes.count(oauth.TOO_MANY) == 4


def _begin_for_test() -> str | None:
    try:
        oauth._begin(None, None, ("person", "owner"), None, google_api.ALL_PROJECTS)
    except oauth.GoogleSignInError as error:
        return str(error)
    return None


async def test_callback_uses_browser_copy_without_agent_tool_names(monkeypatch):
    errors = (
        oauth.GoogleSignInError(oauth.DENIED, browser_message=oauth.BROWSER_DENIED),
        oauth.GoogleSignInError(oauth.MISSING_SCOPES, browser_message=oauth.BROWSER_MISSING_SCOPES),
        oauth.GoogleSignInError(
            oauth.TOO_MANY, browser_message="Too many Google sign-ins are waiting. Go back to CORSIA and try again later.",
        ),
        oauth.GoogleSignInError(
            oauth.GONE, browser_message="This Google account was removed from CORSIA. Go back to CORSIA and connect it again.",
        ),
        oauth.GoogleSignInError(
            oauth.WRONG_ACCOUNT.format(signed_in="wrong@example.test", address="right@example.test"),
            browser_message="That's a different Google account. Go back to CORSIA and sign in again as right@example.test.",
        ),
    )
    async with _http() as http:
        for error in errors:
            async def fail(*_args, _error=error, **_kwargs):
                raise _error
            monkeypatch.setattr(oauth, "complete_sign_in", fail)
            response = await http.get("/api/google/oauth/callback", params={"code": "code", "state": "state"})
            assert response.status_code == 400
            assert error.browser_message in html.unescape(response.text)
            assert "google_" not in response.text


async def test_routes_need_a_signed_in_caller(google):
    async with _http() as http:
        assert (await http.get("/api/google/status")).status_code == 401
        ok = await http.get("/api/google/status", headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"})
    assert ok.status_code == 200 and ok.json() == {"accounts": []}
