"""Sign-in over HTTP: status, login, logout, and the failure backoff.

These go through the real routes and the real store, because what is being
checked is exactly the seam between them: that a password becomes a token,
that the token then authenticates, and that a refused attempt stays cheap.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import auth as auth_api  # noqa: E402
from auth_fixtures import OWNER_TOKEN, OWNER_USER, TEST_PASSWORD  # noqa: E402
from infra import accounts, token_admin  # noqa: E402

BEARER = "Bearer {}"


@pytest.fixture
def client(isolated_db):
    """A client for the auth routes, with the failure counter reset.

    The counter is process-wide by design, so a test that trips it would
    otherwise make the next test wait.
    """
    auth_api._failures.clear()
    app = FastAPI()
    app.include_router(auth_api.router)
    with TestClient(app) as test_client:
        yield test_client
    auth_api._failures.clear()


class TestStatus:

    def test_reports_that_accounts_exist(self, client):
        response = client.get("/api/auth/status")
        assert response.status_code == 200
        assert response.json() == {"has_users": True}

    def test_reports_an_install_with_no_account(self, client, isolated_db):
        database = accounts._db(None)
        database.execute("DELETE FROM users")
        database.commit()
        assert client.get("/api/auth/status").json() == {"has_users": False}

    def test_is_reachable_without_a_credential(self, client):
        # The login screen has to be able to ask this before anyone can
        # authenticate, so it must not be behind authentication.
        assert client.get("/api/auth/status").status_code == 200


class TestLogin:

    def test_correct_password_returns_a_working_session(self, client):
        response = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": TEST_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user"] == {"id": OWNER_USER, "name": OWNER_USER}
        assert body["expires_at"]
        issued = body["token"]
        assert accounts.resolve_token(issued) == {
            "id": OWNER_USER, "name": OWNER_USER, "token_kind": "session",
        }

    def test_issued_credential_is_a_session_that_expires(self, client):
        response = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": TEST_PASSWORD},
        )
        sessions = [
            row for row in token_admin.list_tokens(OWNER_USER)
            if row["kind"] == "session" and row["label"] == "browser"
        ]
        assert len(sessions) == 1
        assert sessions[0]["label"] == "browser"
        assert sessions[0]["expires_at"]
        assert response.json()["token"] not in str(sessions)

    def test_username_is_normalized(self, client):
        response = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER.upper(), "password": TEST_PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["user"]["id"] == OWNER_USER

    def test_wrong_password_is_refused(self, client):
        response = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": "wrong-but-long"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == auth_api._INVALID

    def test_unknown_account_is_refused_identically(self, client):
        wrong = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": "wrong-but-long"},
        )
        unknown = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "wrong-but-long"},
        )
        # Telling the two apart would make this route a username oracle.
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()

    @pytest.mark.parametrize(
        "body",
        [
            {"username": OWNER_USER},
            {"password": TEST_PASSWORD},
            {"username": "", "password": TEST_PASSWORD},
            {"username": OWNER_USER, "password": ""},
        ],
    )
    def test_incomplete_credentials_are_rejected_before_hashing(self, client, body):
        assert client.post("/api/auth/login", json=body).status_code == 422

    def test_neither_password_nor_token_is_logged(self, client, caplog):
        with caplog.at_level("DEBUG"):
            issued = client.post(
                "/api/auth/login",
                json={"username": OWNER_USER, "password": TEST_PASSWORD},
            ).json()["token"]
            client.post(
                "/api/auth/login",
                json={"username": OWNER_USER, "password": "wrong-but-long"},
            )
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert TEST_PASSWORD not in logged
        assert "wrong-but-long" not in logged
        assert issued not in logged


class TestFailureBackoff:

    def _fail_once(self, client):
        return client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": "wrong-but-long"},
        )

    def test_a_typo_or_two_is_not_penalized(self, client):
        for _ in range(auth_api._FREE_ATTEMPTS):
            assert self._fail_once(client).status_code == 401

    def test_repeated_failures_are_answered_429_with_retry_after(self, client):
        for _ in range(auth_api._FREE_ATTEMPTS + 1):
            assert self._fail_once(client).status_code == 401
        refused = self._fail_once(client)
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 0

    def test_the_penalized_attempt_never_reaches_the_hash(self, client, monkeypatch):
        for _ in range(auth_api._FREE_ATTEMPTS + 1):
            self._fail_once(client)

        def _must_not_run(*args, **kwargs):
            raise AssertionError("a throttled attempt must not cost a hash")

        monkeypatch.setattr(accounts, "check_password", _must_not_run)
        assert self._fail_once(client).status_code == 429

    def test_a_correct_password_clears_the_count(self, client):
        for _ in range(auth_api._FREE_ATTEMPTS):
            self._fail_once(client)
        assert client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": TEST_PASSWORD},
        ).status_code == 200
        assert OWNER_USER not in auth_api._failures

    def test_the_delay_doubles_and_then_stops(self):
        free = auth_api._FREE_ATTEMPTS
        assert auth_api._penalty_seconds(free) == 0
        assert auth_api._penalty_seconds(free + 1) == auth_api._BASE_DELAY_SECONDS
        assert auth_api._penalty_seconds(free + 2) == auth_api._BASE_DELAY_SECONDS * 2
        assert auth_api._penalty_seconds(free + 99) == auth_api._MAX_DELAY_SECONDS

    def test_one_username_does_not_throttle_another(self, client):
        for _ in range(auth_api._FREE_ATTEMPTS + 2):
            self._fail_once(client)
        assert self._fail_once(client).status_code == 429
        other = client.post(
            "/api/auth/login",
            json={"username": "leksi", "password": "wrong-but-long"},
        )
        assert other.status_code == 401

    def test_the_penalty_is_rechecked_once_the_lock_is_held(self, client, monkeypatch):
        """Attempts that queued together cannot all skip a penalty just earned."""
        original = auth_api._refuse_if_penalized
        calls = []

        def _penalized_while_queued(username):
            original(username)
            if not calls:
                # Another queued attempt fails while this one waits.
                auth_api._failures[username] = (
                    auth_api._FREE_ATTEMPTS + 1,
                    auth_api.time.monotonic(),
                    True,
                )
            calls.append(username)

        def _must_not_run(*args, **kwargs):
            raise AssertionError("a penalized attempt must not cost a hash")

        monkeypatch.setattr(auth_api, "_refuse_if_penalized", _penalized_while_queued)
        monkeypatch.setattr(accounts, "check_password", _must_not_run)
        assert self._fail_once(client).status_code == 429

    def test_made_up_names_beyond_the_waiting_cap_are_held_then_refused(
        self, client, monkeypatch
    ):
        def _must_not_run(*args, **kwargs):
            raise AssertionError("a refused attempt must not cost a hash")

        monkeypatch.setattr(
            auth_api, "_waiting_attempts", auth_api._MAX_WAITING_ATTEMPTS
        )
        monkeypatch.setattr(auth_api, "_HELD_SECONDS", 0)
        monkeypatch.setattr(accounts, "check_password", _must_not_run)
        refused = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": TEST_PASSWORD},
        )
        # The same answer as a failed check, so the cap says nothing about the name.
        assert refused.status_code == 401
        assert refused.json()["detail"] == auth_api._INVALID
        assert auth_api._failures["nobody"][0] == 1

    def test_a_full_waiting_cap_does_not_keep_the_owner_out(self, client, monkeypatch):
        monkeypatch.setattr(
            auth_api, "_waiting_attempts", auth_api._MAX_WAITING_ATTEMPTS
        )
        signed_in = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": TEST_PASSWORD},
        )
        assert signed_in.status_code == 200

    def test_a_full_waiting_cap_holds_existing_and_made_up_names_for_the_same_minimum(self, client, monkeypatch):
        holds = []

        async def _hold(seconds):
            holds.append(seconds)

        monkeypatch.setattr(
            auth_api, "_waiting_attempts", auth_api._MAX_WAITING_ATTEMPTS
        )
        monkeypatch.setattr(auth_api.asyncio, "sleep", _hold)
        made_up = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": TEST_PASSWORD},
        )
        existing = client.post(
            "/api/auth/login",
            json={"username": OWNER_USER, "password": TEST_PASSWORD},
        )

        assert made_up.status_code == 401
        assert existing.status_code == 200
        assert holds == [auth_api._HELD_SECONDS, auth_api._HELD_SECONDS]

    def test_one_waiting_attempt_per_name(self, client, monkeypatch):
        def _must_not_run(*args, **kwargs):
            raise AssertionError("a refused attempt must not cost a hash")

        monkeypatch.setattr(auth_api, "_waiting_names", {OWNER_USER})
        monkeypatch.setattr(accounts, "check_password", _must_not_run)
        refused = self._fail_once(client)
        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "1"

    def test_a_malformed_username_is_refused_without_being_tracked(self, client):
        refused = client.post(
            "/api/auth/login",
            json={"username": "not a valid name!", "password": "wrong-but-long"},
        )
        assert refused.status_code == 401
        assert auth_api._failures == {}

    def test_tracked_usernames_stay_bounded(self, monkeypatch):
        monkeypatch.setattr(auth_api, "_MAX_TRACKED_USERNAMES", 3)
        for name in ("a", "b", "c", "d", "e"):
            auth_api._record_failure(name, is_account=False)
        assert list(auth_api._failures) == ["c", "d", "e"]

    def test_made_up_names_never_push_out_an_account(self, monkeypatch):
        monkeypatch.setattr(auth_api, "_MAX_TRACKED_USERNAMES", 3)
        auth_api._record_failure(OWNER_USER, is_account=True)
        for name in ("a", "b", "c", "d", "e"):
            auth_api._record_failure(name, is_account=False)
        assert list(auth_api._failures) == [OWNER_USER, "d", "e"]


class TestLogout:

    def test_revokes_exactly_the_presented_token(self, client):
        other, _ = accounts.issue_token(OWNER_USER, kind="api", label="keep")
        response = client.post(
            "/api/auth/logout",
            headers={"Authorization": BEARER.format(OWNER_TOKEN)},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert accounts.resolve_token(OWNER_TOKEN) is None
        assert accounts.resolve_token(other) is not None

    def test_without_a_credential_is_refused(self, client):
        assert client.post("/api/auth/logout").status_code == 401

    def test_with_an_unknown_credential_is_refused(self, client):
        response = client.post(
            "/api/auth/logout",
            headers={"Authorization": BEARER.format("no-such-value")},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or expired session"

    def test_is_not_replayable(self, client):
        headers = {"Authorization": BEARER.format(OWNER_TOKEN)}
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.post("/api/auth/logout", headers=headers).status_code == 401


class TestRouteRegistration:

    def test_the_app_serves_the_auth_routes(self):
        # Routes are included lazily, so read the paths through each included
        # router rather than off ``app.routes`` directly.
        from main import app

        paths = set()
        for route in app.routes:
            included = getattr(route, "original_router", None)
            for entry in getattr(included, "routes", []) if included else [route]:
                path = getattr(entry, "path", None)
                if path:
                    paths.add(path)
        assert {
            "/api/auth/status", "/api/auth/login", "/api/auth/logout",
        } <= paths
