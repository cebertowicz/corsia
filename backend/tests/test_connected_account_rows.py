"""Google accounts as Connected apps rows: the list, and Sign out, Remove and Where it works for one row."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import connected_account_rows, connected_apps
from api.connected_account_rows import NOT_FOUND, PERSON_ONLY, PERSON_ONLY_CONTROLS
from auth_fixtures import OWNER_USER
from connections.app_reach import AppReach
from connections.app_lifecycle import create_app
from connections.connected_accounts import (
    GOOGLE_PROVIDER, create_google_account, credential_key, get_account, store_google_credentials,
)
from connections.google import account_actions
from google_account_helpers import ONLY_CORSIA_REACH, make_only_corsia
from infra.auth import require_user
from infra.database import get_db

PERSON = {"id": OWNER_USER, "token_kind": "session"}
AGENT = {"id": OWNER_USER, "thread_id": "thread-a", "token_kind": "internal"}
ALL = {"mode": "all", "project_ids": [], "outside_projects": False}
SECRETS = ("client-id-value", "client-secret-value", "refresh-token-value", "connected-account--google--")


def _stored_entries(account_id: str) -> int:
    db = get_db()
    try:
        return db.execute("SELECT COUNT(*) FROM key_store_entries WHERE name=?", (credential_key(account_id),)).fetchone()[0]
    finally:
        db.close()


def _account(address: str, *, reach: AppReach | None = None, signed_in: bool = True) -> str:
    """A connected account; with no ``reach``, Only CORSIA."""
    account_id = str(create_google_account(
        address, reach=reach or ALL, created_by=("system", "test"), state="connected",
    )["id"])
    if reach is None:
        make_only_corsia(account_id)
    if signed_in:
        store_google_credentials(account_id, {
            "client_id": SECRETS[0], "client_secret": SECRETS[1], "refresh_token": SECRETS[2],
            "scopes": ["https://mail.google.com/"],
        })
    return account_id


@pytest.fixture
def api(isolated_db, monkeypatch):
    db = get_db()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('project-a','a','t','t')")
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('project-b','b','t','t')")
    for thread_id, project_id in (("thread-a", "project-a"), ("thread-b", "project-b")):
        db.execute(
            "INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, OWNER_USER, project_id, "t", "t"),
        )
    db.commit()
    revoked: list[str] = []
    real_revoke = account_actions._revoke

    async def revoke(token):
        if not token:
            return await real_revoke(token)
        revoked.append(token)
        return "revoked"

    monkeypatch.setattr(account_actions, "_revoke", revoke)
    who = {"user": PERSON}
    server = FastAPI()
    server.include_router(connected_apps.connected_apps_router, prefix="/api/mcp")
    server.dependency_overrides[require_user] = lambda: who["user"]
    return TestClient(server), who, revoked


def _rows(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/mcp/servers")
    assert response.status_code == 200
    return {row["address"]: row for row in response.json()["accounts"]}


def test_two_accounts_are_two_rows_with_their_own_state_and_reach_and_no_secrets(api):
    client, _who, _revoked = api
    first = _account("person@example.com")
    second = _account("work@example.com", reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False), signed_in=False)
    create_app("Linear", {"url": "https://mcp.example/mcp", "key_refs": []}, ALL, db=get_db())

    response = client.get("/api/mcp/servers")

    body = response.json()
    assert [server["name"] for server in body["servers"]] == ["Linear"]
    rows = {row["id"]: row for row in body["accounts"]}
    assert set(rows) == {first, second}
    assert rows[first] == {
        "kind": "account", "id": first, "provider": GOOGLE_PROVIDER, "address": "person@example.com",
        "capabilities": ["mail", "calendar", "files"], "state": "connected", "reach": ONLY_CORSIA_REACH,
    }
    # A row that lost its sign-in says so, with its own reach, whatever its stored state.
    assert rows[second]["state"] == "needs_sign_in" and rows[second]["reach"] == ALL
    assert not any(secret in response.text for secret in SECRETS)


def test_the_list_filters_accounts_like_apps_and_an_agent_sees_only_those_that_reach_it(api):
    client, who, _revoked = api
    only_corsia = _account("corsia@example.com")
    in_a = _account("a@example.com", reach=AppReach(mode="selected", project_ids=frozenset(("project-a",)), outside_projects=False))
    everywhere = _account("all@example.com", reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))

    assert {row["id"] for row in client.get("/api/mcp/servers").json()["accounts"]} == {only_corsia, in_a, everywhere}
    assert {row["id"] for row in client.get("/api/mcp/servers?project=project-b").json()["accounts"]} == {everywhere}
    assert {row["id"] for row in client.get("/api/mcp/servers?place=workspace").json()["accounts"]} == {everywhere}

    who["user"] = AGENT
    assert {row["id"] for row in client.get("/api/mcp/servers").json()["accounts"]} == {in_a, everywhere}


def test_sign_out_changes_only_that_row_and_returns_it_with_the_cause(api):
    client, _who, revoked = api
    first = _account("person@example.com")
    second = _account("work@example.com")

    response = client.post(f"/api/mcp/accounts/{first}/sign-out")

    assert response.status_code == 200
    assert response.json()["revocation"] == "revoked" and response.json()["row"]["state"] == "signed_out"
    assert response.json()["row"]["id"] == first
    assert revoked == [SECRETS[2]]
    rows = _rows(client)
    assert rows["person@example.com"]["state"] == "signed_out" and rows["work@example.com"]["state"] == "connected"
    assert not any(secret in response.text for secret in SECRETS[:3])
    # It can't be signed out twice.
    again = client.post(f"/api/mcp/accounts/{first}/sign-out")
    assert again.status_code == 409 and again.json()["detail"] == connected_account_rows.NOT_SIGNED_IN
    assert _rows(client)["work@example.com"]["state"] == "connected" and second


def test_remove_deletes_only_that_row_and_its_credentials(api):
    client, _who, revoked = api
    first = _account("person@example.com")
    second = _account("work@example.com")

    response = client.delete(f"/api/mcp/accounts/{first}")

    assert response.json() == {"id": first, "removed": True, "revocation": "revoked"}
    assert revoked == [SECRETS[2]]
    assert list(_rows(client)) == ["work@example.com"]
    db = get_db()
    assert (_stored_entries(first), _stored_entries(second)) == (0, 1)
    assert get_account(second, db=db)["state"] == "connected"
    audits = db.execute("SELECT actor_kind,actor_id,action,revocation_cause FROM connected_account_audit WHERE account_id=?", (first,)).fetchall()
    assert [tuple(row) for row in audits] == [("person", OWNER_USER, "remove", "revoked")]


def test_removing_a_signed_out_row_says_it_was_not_signed_in_and_never_asks_for_the_manual_step(api):
    client, _who, revoked = api
    account_id = _account("person@example.com")
    assert client.post(f"/api/mcp/accounts/{account_id}/sign-out").json()["revocation"] == "revoked"

    response = client.delete(f"/api/mcp/accounts/{account_id}")

    assert response.json() == {"id": account_id, "removed": True, "revocation": "not_signed_in"}
    assert "not_signed_in" not in account_actions.MANUAL_STEP_CAUSES
    assert revoked == [SECRETS[2]]  # The one revoke ran at Sign out.
    assert _rows(client) == {}


def test_the_manual_step_causes_are_exactly_the_provider_failures():
    from connections.connected_accounts import REVOCATION_CAUSES

    assert account_actions.MANUAL_STEP_CAUSES == {
        "provider_unreachable", "provider_timeout", "provider_rejected", "provider_unexpected",
    }
    assert REVOCATION_CAUSES - account_actions.MANUAL_STEP_CAUSES == {"revoked", "not_signed_in"}
    assert "manual_only" not in REVOCATION_CAUSES


def test_an_agent_cannot_widen_where_an_account_works_but_can_narrow_it(api):
    client, who, _revoked = api
    both = {"mode": "selected", "project_ids": ["project-a"], "outside_projects": True}
    account_id = _account("a@example.com", reach=AppReach(mode="selected", project_ids=frozenset(("project-a",)), outside_projects=True))
    who["user"] = AGENT

    widened = client.put(f"/api/mcp/accounts/{account_id}/reach", json={"reach": ALL})
    assert widened.status_code == 403 and widened.json()["detail"] == PERSON_ONLY
    assert widened.headers["X-Corsia-Agent-Remedy"] == PERSON_ONLY
    assert _rows(client)["a@example.com"]["reach"] == both

    narrowed = client.put(
        f"/api/mcp/accounts/{account_id}/reach", json={"reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}},
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["row"]["reach"] == {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}


def test_a_person_widens_and_the_body_cannot_name_who_acted(api):
    client, _who, _revoked = api
    account_id = _account("person@example.com")

    refused = client.put(f"/api/mcp/accounts/{account_id}/reach", json={"reach": ALL, "actor": ["person", "x"]})
    assert refused.status_code == 422 and _rows(client)["person@example.com"]["reach"] == ONLY_CORSIA_REACH

    saved = client.put(f"/api/mcp/accounts/{account_id}/reach", json={"reach": ALL})
    assert saved.status_code == 200 and saved.json()["row"]["reach"] == ALL
    assert _rows(client)["person@example.com"]["reach"] == ALL
    # Sign out audits the session's actor, whatever the body says.
    client.request("POST", f"/api/mcp/accounts/{account_id}/sign-out", content=json.dumps({"actor": ["person", "x"]}))
    db = get_db()
    assert [tuple(row) for row in db.execute("SELECT actor_kind,actor_id FROM connected_account_audit WHERE action='sign_out'")] == [("person", OWNER_USER)]


@pytest.mark.parametrize("reach,detail", [
    ({"mode": "selected", "project_ids": [], "outside_projects": False}, connected_account_rows.EMPTY_REACH),
    # Only CORSIA is never saved: only the legacy migration writes it.
    (ONLY_CORSIA_REACH, connected_account_rows.INVALID_REACH),
    ({"mode": "nope"}, connected_account_rows.INVALID_REACH),
])
def test_where_it_works_refuses_a_choice_that_names_no_place_or_is_invalid(api, reach, detail):
    client, _who, _revoked = api
    account_id = _account("person@example.com", reach=AppReach(mode="all", project_ids=frozenset(), outside_projects=False))

    response = client.put(f"/api/mcp/accounts/{account_id}/reach", json={"reach": reach})

    assert response.status_code == 422 and response.json()["detail"] == detail
    assert _rows(client)["person@example.com"]["reach"] == ALL


def test_an_agent_narrowing_to_no_place_is_refused_like_any_empty_choice(api):
    client, who, _revoked = api
    account_id = _account("a@example.com", reach=AppReach(mode="selected", project_ids=frozenset(("project-a",)), outside_projects=False))
    who["user"] = AGENT

    response = client.put(
        f"/api/mcp/accounts/{account_id}/reach", json={"reach": {"mode": "selected", "project_ids": [], "outside_projects": False}},
    )

    assert response.status_code == 422 and response.json()["detail"] == connected_account_rows.EMPTY_REACH


@pytest.mark.parametrize("method,path,body", [
    ("post", "/sign-out", None), ("delete", "", None), ("put", "/reach", {"reach": ALL}),
])
def test_an_unknown_or_non_google_id_is_a_fixed_404(api, method, path, body):
    client, _who, _revoked = api
    db = get_db()
    db.execute(
        "INSERT INTO connected_accounts(id,provider,address,capabilities_json,state,reach_mode,credential_key,"
        "created_by_kind,created_by_id,created_at,updated_at) VALUES ('other-1','microsoft','m@example.com','[]',"
        "'connected','selected','connected-account--microsoft--other-1','system','test','t','t')",
    )
    db.commit()
    for account_id in ("no-such-id", "other-1"):
        response = getattr(client, method)(f"/api/mcp/accounts/{account_id}{path}", **({"json": body} if body else {}))
        assert response.status_code == 404 and response.json()["detail"] == NOT_FOUND
    assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id='other-1'").fetchone()[0] == 1


@pytest.mark.parametrize("reaches_thread", [True, False])
def test_an_agent_cannot_sign_out_or_remove_any_account_and_nothing_changes(api, reaches_thread):
    client, who, revoked = api
    reach = AppReach(mode="selected", project_ids=frozenset(("project-a" if reaches_thread else "project-b",)), outside_projects=False)
    account_id = _account("person@example.com", reach=reach)
    before = get_account(account_id, db=get_db())
    who["user"] = AGENT

    for response in (client.post(f"/api/mcp/accounts/{account_id}/sign-out"), client.delete(f"/api/mcp/accounts/{account_id}")):
        # Out of reach, the account answers exactly like an unknown id.
        expected = (403, PERSON_ONLY_CONTROLS) if reaches_thread else (404, NOT_FOUND)
        assert (response.status_code, response.json()["detail"]) == expected
        if reaches_thread:
            assert response.headers["X-Corsia-Agent-Remedy"] == PERSON_ONLY_CONTROLS
    assert revoked == [] and _stored_entries(account_id) == 1
    assert get_account(account_id, db=get_db()) == before
    db = get_db()
    assert db.execute("SELECT COUNT(*) FROM connected_account_audit WHERE account_id=?", (account_id,)).fetchone()[0] == 0

    who["user"] = PERSON
    assert client.post(f"/api/mcp/accounts/{account_id}/sign-out").json()["row"]["state"] == "signed_out"
    assert client.delete(f"/api/mcp/accounts/{account_id}").json()["removed"] is True


def test_an_agent_gets_the_404_before_the_person_only_refusal(api):
    client, who, _revoked = api
    who["user"] = AGENT
    for response in (client.post("/api/mcp/accounts/no-such-id/sign-out"), client.delete("/api/mcp/accounts/no-such-id")):
        assert response.status_code == 404 and response.json()["detail"] == NOT_FOUND


@pytest.mark.parametrize("method,path,body", [
    ("post", "/sign-out", None), ("delete", "", None),
    ("put", "/reach", {"reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}}),
])
def test_an_out_of_reach_account_answers_every_row_route_exactly_like_an_unknown_id(api, method, path, body):
    client, who, _revoked = api
    out_of_reach = _account("person@example.com")  # Only CORSIA: no project reaches it.
    before = get_account(out_of_reach, db=get_db())
    who["user"] = AGENT

    def call(account_id: str):
        return getattr(client, method)(f"/api/mcp/accounts/{account_id}{path}", **({"json": body} if body else {}))

    hidden, unknown = call(out_of_reach), call("no-such-id")

    assert hidden.status_code == unknown.status_code == 404
    assert hidden.content == unknown.content
    assert hidden.headers.get("X-Corsia-Agent-Remedy") == unknown.headers.get("X-Corsia-Agent-Remedy")
    assert get_account(out_of_reach, db=get_db()) == before
