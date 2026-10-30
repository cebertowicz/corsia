"""Several Google accounts side by side: concurrent sign-ins, identity dedupe, per-account
refresh failure, Sign out, Remove, and who may widen where an account works."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from connections.app_reach import AppReach
from connections.connected_accounts import (
    GOOGLE_PROVIDER, ConnectedAccountError, create_account, credential_key, get_account, resolve_credentials,
)
from connections import account_actions
from connections.google import provider as google_provider
from connections.google import client as google_client
from connections.google import oauth
from google_account_helpers import ONLY_CORSIA_REACH, only_corsia_account
from infra.database import get_db
from tools.google_account import GoogleConnectTool
from tests.test_google_sign_in import (  # noqa: F401 -- conductor and client_file are fixtures
    ACCESS, REFRESH, _REAL_CLIENT, FakeGoogle, _accounts, _http, _id_token, _start, client_file, conductor,
)

PERSON = ("person", "steve")
LABELS = "https://gmail.googleapis.com/gmail/v1/users/me/labels"


class AccountsGoogle(FakeGoogle):
    """FakeGoogle that answers with the current address, issues a distinct sign-in per
    code exchange, refuses refreshes of chosen sign-ins, and records revokes."""

    def __init__(self) -> None:
        super().__init__()
        self.email = "person@example.com"
        self.issued = 0
        self.refused: set[str] = set()
        self.revoke_status = 200
        self.revoked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if url == google_provider.REVOKE_URL:
            self.revoked.append(form["token"])
            return httpx.Response(self.revoke_status, text="google says no, token " + form["token"])
        if url == oauth.PROFILE_URL:
            return httpx.Response(200, json={"emailAddress": self.email})
        if url == google_client.TOKEN_URL and form.get("grant_type") == "refresh_token" and form["refresh_token"] in self.refused:
            return httpx.Response(400, json={"error": "invalid_grant"})
        response = super().handler(request)
        if url == google_client.TOKEN_URL and form.get("grant_type") == "authorization_code" and response.status_code == 200:
            self.issued += 1
            grant = response.json()
            grant["refresh_token"] = f"sign-in-{self.issued}"
            return httpx.Response(200, json=grant)
        return response


@pytest.fixture
def google(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    fake = AccountsGoogle()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(google_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(oauth, "_pending", {})
    return fake


async def _finish(google: AccountsGoogle, state: str, challenge: str, email: str) -> httpx.Response:
    google.challenge, google.email = challenge, email
    async with _http() as http:
        return await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})


async def _add(google: AccountsGoogle, conductor: str, client_file: str | None, email: str) -> str:
    before = {account["id"] for account in _accounts()}
    _, state = await _start(google, conductor, client_file)
    response = await _finish(google, state, google.challenge, email)
    assert response.status_code == 200, response.text
    [added] = [account["id"] for account in _accounts() if account["id"] not in before]
    return str(added)


def _row(account_id: str) -> dict:
    db = get_db()
    try:
        return get_account(account_id, db=db)
    finally:
        db.close()


def _bundle(account_id: str) -> dict | None:
    db = get_db()
    try:
        return resolve_credentials(account_id, db=db)
    finally:
        db.close()


def _audits(account_id: str) -> list[tuple]:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT actor_kind, actor_id, action, revocation_cause FROM connected_account_audit"
            " WHERE account_id=? ORDER BY id", (account_id,),
        ).fetchall()
        return [tuple(row) for row in rows]
    finally:
        db.close()


def _stored_entry_count(account_id: str) -> int:
    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM key_store_entries WHERE name=?", (credential_key(account_id),),
        ).fetchone()[0]
    finally:
        db.close()


async def test_two_sign_ins_in_flight_each_add_their_own_account(google, conductor, client_file):
    _, first = await _start(google, conductor, client_file)
    first_challenge = google.challenge
    _, second = await _start(google, conductor, client_file)
    second_challenge = google.challenge
    # The person finishes the second browser tab first.
    assert (await _finish(google, second, second_challenge, "work@example.com")).status_code == 200
    assert (await _finish(google, first, first_challenge, "person@example.com")).status_code == 200

    by_address = {account["address"]: str(account["id"]) for account in _accounts()}
    assert set(by_address) == {"person@example.com", "work@example.com"}
    assert _bundle(by_address["work@example.com"])["refresh_token"] == "sign-in-1"
    assert _bundle(by_address["person@example.com"])["refresh_token"] == "sign-in-2"
    for account_id in by_address.values():
        assert _audits(account_id) == [("agent", conductor, "connect", None)]


async def test_the_same_google_identity_under_another_address_is_not_added_twice(google, conductor):
    async def published_finish(email):
        out = await GoogleConnectTool().execute(_thread_id=conductor)
        state = json.loads(base64.b64decode(parse_qs(urlparse(json.loads(out)["url"]).query)["state"][0]))["csrf"]
        google.email = email
        async with _http() as http:
            return await http.get("/api/google/oauth/callback", params={
                "state": state, "access_token": ACCESS, "refresh_token": REFRESH,
                "scope": " ".join(oauth.PUBLISHED_REQUIRED_SCOPES),
            })
    google.id_token = _id_token("subject-one")
    assert (await published_finish("person@example.com")).status_code == 200
    first = str(_accounts()[0]["id"])
    again = await published_finish("alias@example.com")
    assert again.status_code == 400 and "person@example.com is already connected." in again.text
    assert [str(account["id"]) for account in _accounts()] == [first]

    google.id_token = _id_token("subject-two")
    # A different Google identity is a new account, even one reusing nothing else.
    assert (await published_finish("other@example.com")).status_code == 200
    assert len(_accounts()) == 2


async def test_a_refused_refresh_signs_out_only_that_account(google, conductor, client_file):
    first = await _add(google, conductor, client_file, "person@example.com")
    second = await _add(google, conductor, client_file, "work@example.com")
    google.refused.add(_bundle(first)["refresh_token"])

    with pytest.raises(google_client.GoogleSignInRejected):
        await google_client.get_json(first, LABELS)
    assert await google_client.get_json(second, LABELS) == {"ok": True}

    assert _row(first)["state"] == "needs_sign_in"
    assert _row(second)["state"] == "connected"
    with pytest.raises(google_client.GoogleNotConnected):
        await google_client.get_json(first, LABELS)


async def test_google_lifecycle_events_are_emitted_once(google, conductor, client_file, monkeypatch):
    from connections import account_state

    events: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        account_state, "emit_product_event",
        lambda category, action, **kwargs: events.append((category, action, kwargs["properties"])),
    )
    account_id = await _add(google, conductor, client_file, "person@example.com")
    await account_actions.sign_out_account(account_id, actor=PERSON)
    await account_actions.remove_account(account_id, actor=PERSON)
    _, state = await _start(google, conductor, client_file)
    with pytest.raises(google_client.GoogleSignInError):
        await oauth.complete_sign_in(None, state)

    assert [(category, action) for category, action, _ in events] == [
        ("connected_account", "connect_started"),
        ("connected_account", "connect_succeeded"),
        ("connected_account", "signed_out"),
        ("connected_account", "removed"),
        ("connected_account", "connect_started"),
        ("connected_account", "connect_failed"),
    ]
    assert all(event["provider"] == GOOGLE_PROVIDER for _, _, event in events)
    assert events[-1][2]["cause"] == "consent_cancelled"


async def test_sign_out_leaves_no_usable_credential_and_audits_the_revoke(google, conductor, client_file):
    account_id = await _add(google, conductor, client_file, "person@example.com")
    token = _bundle(account_id)["refresh_token"]

    result = await account_actions.sign_out_account(account_id, actor=PERSON)

    assert result == {"account_id": account_id, "state": "signed_out", "revocation": "revoked"}
    assert google.revoked == [token]
    assert _row(account_id)["state"] == "signed_out"
    assert _bundle(account_id) is None
    with pytest.raises(google_client.GoogleNotConnected):
        await google_client.get_json(account_id, LABELS)
    assert _audits(account_id)[-1] == ("person", "steve", "sign_out", "revoked")
    # The row and its saved client stay, so Sign in needs no client file.
    url, _ = await oauth.start_sign_in(actor=PERSON, account_id=account_id)
    assert url.startswith(oauth.AUTH_URL)


@pytest.mark.parametrize(("status", "cause"), [(400, "provider_rejected"), (503, "provider_unexpected")])
async def test_a_remove_whose_revoke_fails_still_removes_with_a_fixed_cause(
    google, conductor, client_file, status, cause,
):
    account_id = await _add(google, conductor, client_file, "person@example.com")
    kept = await _add(google, conductor, client_file, "work@example.com")
    google.revoke_status = status

    result = await account_actions.remove_account(account_id, actor=PERSON)

    assert result == {"account_id": account_id, "removed": True, "revocation": cause}
    assert [str(account["id"]) for account in _accounts()] == [kept]
    assert _stored_entry_count(account_id) == 0
    assert _audits(account_id)[-1] == ("person", "steve", "remove", cause)
    db = get_db()
    try:
        stored = json.dumps([tuple(row) for row in db.execute("SELECT * FROM connected_account_audit")])
    finally:
        db.close()
    assert "google says no" not in stored
    assert _row(kept)["state"] == "connected"


async def test_a_remove_that_cannot_reach_google_still_removes(google, conductor, client_file, monkeypatch):
    account_id = await _add(google, conductor, client_file, "person@example.com")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(google, "handler", unreachable)
    result = await account_actions.remove_account(account_id, actor=PERSON)
    assert result["revocation"] == "provider_unreachable"
    assert _accounts() == []


def test_only_the_person_can_widen_where_an_account_works(isolated_db):
    account_id = str(only_corsia_account("person@example.com", created_by=("system", "test"), state="connected")["id"])
    everywhere = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)
    outside = AppReach(mode="selected", project_ids=frozenset(), outside_projects=True)

    with pytest.raises(ConnectedAccountError, match=account_actions.PERSON_ONLY):
        account_actions.change_account_reach(account_id, everywhere, actor=("agent", "some-thread"))
    assert _row(account_id)["reach"] == ONLY_CORSIA_REACH

    account_actions.change_account_reach(account_id, everywhere, actor=PERSON)
    assert _row(account_id)["reach"]["mode"] == "all"
    # Narrowing never needs the person.
    account_actions.change_account_reach(account_id, outside, actor=("agent", "some-thread"))
    assert _row(account_id)["reach"] == outside.as_dict()
    # Narrowing to no place is refused, and Only CORSIA is never a value a save can write.
    nowhere = AppReach(mode="selected", project_ids=frozenset(), outside_projects=False)
    for actor in (PERSON, ("agent", "some-thread")):
        with pytest.raises(ConnectedAccountError, match=account_actions.EMPTY_REACH):
            account_actions.change_account_reach(account_id, nowhere, actor=actor)
        with pytest.raises(ConnectedAccountError, match=account_actions.INVALID_REACH):
            account_actions.change_account_reach(account_id, ONLY_CORSIA_REACH, actor=actor)
    assert _row(account_id)["reach"] == outside.as_dict()


def test_the_remove_audit_survives_with_foreign_keys_on(isolated_db):
    """Remove deletes the account row; its audit record must outlive it."""
    account_id = str(only_corsia_account("person@example.com", created_by=("system", "test"), state="connected")["id"])
    db = get_db()
    try:

        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        account_actions._remove_locally(account_id, PERSON, db=db)
        assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id=?", (account_id,)).fetchone()[0] == 0

    finally:
        db.close()
    assert _audits(account_id) == [("person", "steve", "remove", None)]
