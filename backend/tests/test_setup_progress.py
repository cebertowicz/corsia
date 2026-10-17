"""The backend owns where first-run setup stands, so a reload resumes it and a finished setup never reopens."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import setup as setup_api  # noqa: E402
from infra import accounts, setup_progress  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402

BASE = "http:" + f"//127.0.0.1:{DEFAULT_BACKEND_PORT}"
HOST = {"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}"}
JSON_HEADERS = {**HOST, "Content-Type": "application/json"}
ACCOUNT = {"name": "First", "username": "first", "password": "a-long-enough-password"}


@pytest.fixture
def facts(monkeypatch):
    """The eligibility facts `_setup_status` reads now; a test changes them to prove they are not stored."""
    current = {"local_eligible": True, "free_disk_gb": 120.0, "existing_omlx": False}
    monkeypatch.setattr(setup_api, "_setup_status", lambda: dict(current))
    return current


@pytest.fixture
def client(isolated_db, tmp_path, monkeypatch, facts):
    from infra import config

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    database = accounts._db(None)
    database.execute("DELETE FROM users")
    database.commit()
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 50100)) as test_client:
        yield test_client


def _put(client, body: dict, token: str | None = None):
    headers = {**JSON_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {})}
    return client.put("/api/setup/progress", json=body, headers=headers)


def _progress(client, token: str) -> dict:
    response = client.get("/api/setup/progress", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return response.json()


def _create_account(client) -> str:
    response = client.post("/api/setup/first-account", json=ACCOUNT, headers=JSON_HEADERS)
    assert response.status_code == 200
    return response.json()["token"]


def test_a_new_account_reopens_setup_at_connect_an_ai(client):
    token = _create_account(client)

    progress = _progress(client, token)

    assert progress["setup_completed"] is False
    assert progress["step"] == "models"
    assert progress["local_ai"] is None


@pytest.mark.parametrize("step", ["models", "local-consent"])
def test_a_reload_after_the_account_lands_on_the_same_step_with_the_same_choice(client, step):
    assert _put(client, {"local_ai": False}).status_code == 200  # chosen on Welcome, before any account
    token = _create_account(client)
    assert _put(client, {"step": step}, token).status_code == 200

    for _reload in range(2):
        progress = _progress(client, token)
        assert (progress["setup_completed"], progress["step"], progress["local_ai"]) == (False, step, False)


def test_the_local_choice_changes_after_the_account_and_survives_a_reload(client):
    token = _create_account(client)
    assert _put(client, {"local_ai": True}, token).status_code == 200
    assert _put(client, {"local_ai": False}, token).status_code == 200

    assert _progress(client, token)["local_ai"] is False


def test_eligibility_facts_are_read_fresh_each_time_never_replayed(client, facts):
    token = _create_account(client)
    assert _progress(client, token)["status"]["free_disk_gb"] == 120.0

    facts["free_disk_gb"] = 4.0
    facts["existing_omlx"] = True

    assert _progress(client, token)["status"] == {"local_eligible": True, "free_disk_gb": 4.0, "existing_omlx": True}
    assert "free_disk_gb" not in (setup_progress.read() or {})


def test_a_finished_setup_never_reopens_even_with_no_model(client):
    token = _create_account(client)
    assert _put(client, {"completed": True}, token).status_code == 200

    assert _progress(client, token) == {"setup_completed": True, "step": None, "local_ai": None}
    # A late step write, as from a slow tab, cannot reopen it.
    assert _put(client, {"step": "models"}, token).status_code == 200
    assert _progress(client, token)["setup_completed"] is True


def test_an_install_with_no_record_and_an_account_counts_as_completed(client):
    accounts.create_user("existing", "a-long-enough-password")
    token, _expires = accounts.issue_token("existing", kind="session", label="browser")

    assert setup_progress.read() is None
    assert _progress(client, token) == {"setup_completed": True, "step": None, "local_ai": None}
    # An old install cannot be dropped into setup by a step write either.
    assert _put(client, {"step": "models"}, token).status_code == 200
    assert _progress(client, token)["setup_completed"] is True


def test_an_account_made_by_the_cli_has_no_setup_to_resume(client):
    assert _put(client, {"local_ai": True}).status_code == 200
    accounts.create_first_account("cliowner", "a-long-enough-password", first_run_wake=False)
    token, _expires = accounts.issue_token("cliowner", kind="session", label="browser")

    assert _progress(client, token)["setup_completed"] is True


def test_before_an_account_only_the_local_choice_is_taken(client):
    assert _put(client, {"step": "models"}).status_code == 422
    assert _put(client, {"completed": True}).status_code == 422
    assert setup_progress.read() is None


def test_after_an_account_a_write_needs_a_session(client):
    token = _create_account(client)

    assert _put(client, {"local_ai": False}).status_code == 401
    assert _put(client, {"local_ai": False}, "not-a-session").status_code == 401
    assert _progress(client, token)["local_ai"] is None


def test_an_unknown_step_is_refused(client):
    token = _create_account(client)

    assert _put(client, {"step": "finish"}, token).status_code == 422


def test_the_pre_account_write_keeps_the_loopback_boundary(client):
    foreign = client.put("/api/setup/progress", json={"local_ai": True}, headers={
        **JSON_HEADERS, "Host": f"example.com:{DEFAULT_BACKEND_PORT}",
    })

    assert foreign.status_code == 403
    assert setup_progress.read() is None


def test_reading_progress_needs_a_session(client):
    assert client.get("/api/setup/progress", headers=HOST).status_code == 401


def test_update_reads_and_writes_inside_one_write_transaction(isolated_db, monkeypatch):
    """Overlapping changes queue on the write lock, so a late step cannot undo a completion that landed first."""
    setup_progress.update(step="models")
    in_transaction: list[bool] = []
    real_read = setup_progress.read

    def read(*, db=None):
        in_transaction.append(bool(db is not None and db.in_transaction))
        return real_read(db=db)

    monkeypatch.setattr(setup_progress, "read", read)
    setup_progress.update(local_ai=True)

    assert in_transaction == [True]
