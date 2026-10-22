"""Back on setup's account screen edits the account the person already made: only what differs is written."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import setup as setup_api  # noqa: E402
from infra import accounts, config, setup_progress, user_profile  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402

CREATE = {"name": "Pat Example", "username": "pat", "password": "first-password-1", "email": "pat@example.test"}
HEADERS = {"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}", "Content-Type": "application/json"}


@pytest.fixture
def workspace(isolated_db, tmp_path, monkeypatch):
    database = accounts._db(None)
    database.execute("DELETE FROM users")
    database.commit()
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(user_profile, "mac_timezone", lambda: "America/Los_Angeles")
    return tmp_path


@pytest.fixture
def client(workspace):
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(app, base_url=f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}", client=("127.0.0.1", 50100)) as test_client:
        yield test_client


@pytest.fixture
def session(client):
    """The first account, made the way the app makes it, and its session token."""
    response = client.post("/api/setup/first-account", json=CREATE, headers=HEADERS)
    assert response.status_code == 200
    return response.json()["token"]


def _put(client, token, body):
    return client.put("/api/setup/account", json=body, headers={**HEADERS, "Authorization": f"Bearer {token}"})


def _profile(workspace) -> str:
    return (workspace / user_profile.PROFILE_RELATIVE_PATH).read_text(encoding="utf-8")


def test_a_new_name_replaces_the_full_and_preferred_name_and_keeps_every_other_line(client, workspace, session):
    path = workspace / user_profile.PROFILE_RELATIVE_PATH
    path.write_text(path.read_text(encoding="utf-8") + "- **Use community improvements:** yes\n", encoding="utf-8")
    response = _put(client, session, {"name": "Patricia Example"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    text = _profile(workspace)
    assert "- **Full name:** Patricia Example" in text
    assert "- **Preferred name:** Patricia" in text
    assert "Pat Example" not in text
    assert "- **Email:** pat@example.test" in text
    assert "- **Use community improvements:** yes" in text
    assert "- **Timezone:** America/Los_Angeles" in text


def test_a_new_email_replaces_and_an_empty_one_removes_the_line(client, workspace, session):
    assert _put(client, session, {"email": "pat@new.test"}).status_code == 200
    assert "- **Email:** pat@new.test" in _profile(workspace)
    assert "pat@example.test" not in _profile(workspace)
    assert _put(client, session, {"email": ""}).status_code == 200
    assert "Email" not in _profile(workspace)
    assert _put(client, session, {"email": "again@new.test"}).status_code == 200
    assert "- **Email:** again@new.test" in _profile(workspace)


def test_a_new_password_signs_in_with_it_ends_the_old_session_and_returns_the_next(client, workspace, session):
    response = _put(client, session, {"password": "second-password-2"})
    assert response.status_code == 200
    body = response.json()
    assert body["token"] != session
    assert accounts.resolve_token(session) is None
    assert accounts.resolve_token(body["token"])["id"] == "pat"
    assert accounts.check_password("pat", "second-password-2")
    assert not accounts.check_password("pat", CREATE["password"])
    assert "token" not in _put(client, body["token"], {"name": "Pat Example"}).json()


def test_nothing_changes_for_a_name_that_is_already_there(client, workspace, session):
    before = _profile(workspace)
    assert _put(client, session, {"name": "Pat Example", "email": "pat@example.test"}).status_code == 200
    assert _profile(workspace) == before


@pytest.mark.parametrize("body", [{}, {"name": "   "}, {"username": "other"}, {"password": ""}, {"name": "Pat", "extra": 1}])
def test_a_body_that_changes_nothing_valid_is_refused_and_writes_nothing(client, workspace, session, body):
    before = _profile(workspace)
    assert _put(client, session, body).status_code == 422
    assert _profile(workspace) == before
    assert accounts.check_password("pat", CREATE["password"])


def test_a_short_password_is_refused_and_the_old_one_still_works(client, workspace, session):
    response = _put(client, session, {"password": "short"})
    assert response.status_code == 422
    assert accounts.check_password("pat", CREATE["password"])
    assert accounts.resolve_token(session)["id"] == "pat"


def test_a_caller_without_a_session_is_refused(client, workspace, session):
    assert _put(client, "not-a-token", {"name": "Someone Else"}).status_code == 401
    assert client.put("/api/setup/account", json={"name": "Someone Else"}, headers=HEADERS).status_code == 401
    assert "Someone Else" not in _profile(workspace)


def test_a_finished_setup_refuses_the_change(client, workspace, session):
    setup_progress.update(completed=True)
    response = _put(client, session, {"name": "Patricia Example"})
    assert response.status_code == 409
    assert response.json() == {"code": "setup_complete"}
    assert "Patricia" not in _profile(workspace)


def test_a_failed_save_names_a_reference_and_leaves_the_account_as_it_was(client, workspace, session, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("the database is gone")

    # The next session is issued in the password's own transaction: if it cannot be, nothing changed.
    monkeypatch.setattr("infra.account_update.issue_token", refuse)
    response = _put(client, session, {"name": "Patricia Example", "password": "second-password-2"})
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "setup_failed" and len(body["ref"]) == 6
    assert accounts.check_password("pat", CREATE["password"])
    assert not accounts.check_password("pat", "second-password-2")
    assert accounts.resolve_token(session)["id"] == "pat"
    assert "Patricia" not in _profile(workspace)


def test_an_email_alone_is_refused_rather_than_dropped_when_there_is_no_profile_to_put_it_in(client, workspace, session):
    (workspace / user_profile.PROFILE_RELATIVE_PATH).unlink()
    response = _put(client, session, {"email": "new@example.test"})
    assert response.status_code == 500
    assert len(response.json()["ref"]) == 6
    assert not (workspace / user_profile.PROFILE_RELATIVE_PATH).exists()
