"""The backend owns where first-run setup stands, so a reload resumes it and a finished setup never reopens."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import provider_sources as provider_sources_api  # noqa: E402
from api import setup as setup_api  # noqa: E402
from infra import accounts, setup_progress  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402

BASE = "http:" + f"//127.0.0.1:{DEFAULT_BACKEND_PORT}"
HOST = {"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}"}
JSON_HEADERS = {**HOST, "Content-Type": "application/json"}
ACCOUNT = {"name": "First", "username": "first", "password": "a-long-enough-password"}


HANDOFF = {
    "tools": {
        "claude-code": {"attempt": "install_failed", "cause": "download_unavailable", "ref": "setup-42"},
        "codex": {"attempt": "none"},
    },
}


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
    app.include_router(provider_sources_api.router)
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

    assert _progress(client, token) == {"setup_completed": True, "step": None, "local_ai": None, "picks": None}
    # A late step write, as from a slow tab, cannot reopen it.
    assert _put(client, {"step": "models"}, token).status_code == 200
    assert _progress(client, token)["setup_completed"] is True


def test_an_install_with_no_record_and_an_account_counts_as_completed(client):
    accounts.create_user("existing", "a-long-enough-password")
    token, _expires = accounts.issue_token("existing", kind="session", label="browser")

    assert setup_progress.read() is None
    assert _progress(client, token) == {"setup_completed": True, "step": None, "local_ai": None, "picks": None}
    # An old install cannot be dropped into setup by a step write either.
    assert _put(client, {"step": "models"}, token).status_code == 200
    assert _progress(client, token)["setup_completed"] is True


def test_an_account_made_by_the_cli_has_no_setup_to_resume(client):
    assert _put(client, {"local_ai": True}).status_code == 200
    accounts.create_first_account("cliowner", "a-long-enough-password", first_run_wake=False)
    token, _expires = accounts.issue_token("cliowner", kind="session", label="browser")

    assert _progress(client, token)["setup_completed"] is True


def test_pre_account_local_download_status_exposes_only_the_safe_projection(client, monkeypatch):
    monkeypatch.setattr("onboarding.local_download_state.current", lambda: {
        "state": "failed", "cause": "disk_full", "bytes_to_free": 1024,
    })

    response = client.get("/api/setup/local-download", headers=HOST)

    assert response.status_code == 200
    assert response.json() == {"state": "failed", "cause": "disk_full", "bytes_to_free": 1024, "other_omlx": "none"}


@pytest.mark.parametrize(("result", "downloaded"), [
    ({"ok": False, "cause": "connection", "message": "connection lost"}, False),
    ({"ok": False, "cause": "disk_full", "bytes_to_free": 4_000_000_000, "message": "disk full"}, False),
    ({"ok": False, "cause": "memory", "message": "memory"}, False),
    ({"ok": False, "cause": "other", "ref": "setup-42", "message": "other"}, False),
    ({"ok": False, "cause": "other", "pre_download": True, "message": "setup"}, False),
    ({"ok": False, "cause": "connection", "message": "start"}, True),
], ids=["connection", "disk-full", "memory", "other", "setup-failed", "start-failed"])
def test_signed_in_omlx_source_projects_the_same_terminal_flight_as_local_download(client, monkeypatch, result, downloaded):
    """The authenticated source row uses Welcome's one safe local-flight projection."""
    from llm import local_setup, local_sources

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight(result=result, downloaded=downloaded))
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr(local_sources, "omlx_on_path", lambda: None)

    rail = client.get("/api/setup/local-download", headers=HOST)
    assert rail.status_code == 200
    token = _create_account(client)
    sources = client.get("/api/providers/sources", headers={"Authorization": f"Bearer {token}"})
    assert sources.status_code == 200
    source = next(row for row in sources.json()["sources"] if row["source"] == "omlx")

    assert {key: source.get(key) for key in ("local_state", "setup_error_code", "setup_error_reference", "bytes_to_free")} == {
        "local_state": rail.json()["state"],
        "setup_error_code": rail.json().get("cause"),
        "setup_error_reference": rail.json().get("ref"),
        "bytes_to_free": rail.json().get("bytes_to_free"),
    }


def test_settings_flight_never_projects_local_download_fields(monkeypatch):
    from llm import local_setup, local_sources

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight(
        kind="settings", result={"ok": False, "cause": "other", "ref": "settings-42"},
    ))
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr(local_sources, "omlx_on_path", lambda: None)

    source = local_sources.omlx_source((set(), set()))

    assert not {"local_state", "setup_error_code", "setup_error_reference", "bytes_to_free"} & source.keys()


def test_local_download_applies_the_foreign_process_choice_before_starting(client, monkeypatch):
    from unittest.mock import AsyncMock, Mock, call

    started = AsyncMock(return_value={"ok": True})
    choice = Mock(side_effect=["consent_required", None, None])
    monkeypatch.setattr(setup_api, "apply_foreign_omlx_choice", choice)
    monkeypatch.setattr("llm.local_setup_flow.begin_local_setup", started)

    assert client.post("/api/setup/local-download", json={}, headers=JSON_HEADERS).json() == {"code": "consent_required"}
    kept = client.post("/api/setup/local-download", json={"other_omlx": "keep"}, headers=JSON_HEADERS)
    assert kept.status_code == 200 and kept.json() == {"ok": True}
    stopped = client.post("/api/setup/local-download", json={"other_omlx": "stop"}, headers=JSON_HEADERS)
    assert stopped.status_code == 200 and stopped.json() == {"ok": True}
    assert choice.call_args_list == [
        call(other_omlx=None),
        call(other_omlx="keep"),
        call(other_omlx="stop"),
    ]
    assert started.await_count == 2
    assert started.await_args_list == [call(onboarding=True), call(onboarding=True)]


def test_before_an_account_only_the_local_choice_is_taken(client):
    assert _put(client, {"step": "models"}).status_code == 422
    assert _put(client, {"handoff": HANDOFF}).status_code == 422
    assert _put(client, {"completed": True}).status_code == 422
    assert setup_progress.read() is None


def test_handoff_can_be_written_before_binding_and_never_copies_it(client):
    token = _create_account(client)

    assert _put(client, {"handoff": HANDOFF}, token).status_code == 200
    assert setup_progress.read()["handoff"] == HANDOFF
    assert _put(client, {"completed": True, "handoff": HANDOFF}, token).status_code == 200

    assert setup_progress.read()["handoff"] == HANDOFF
    # A stale tab cannot change either completion or the durable handoff after binding.
    assert _put(client, {"handoff": HANDOFF}, token).status_code == 200
    assert setup_progress.read()["handoff"] == HANDOFF


@pytest.mark.parametrize("handoff", [
    {**HANDOFF, "tools": {"claude-code": {"attempt": "none"}}},
    {**HANDOFF, "tools": {"claude-code": {"attempt": "install_failed"}, "codex": {"attempt": "none"}}},
    {**HANDOFF, "tools": {"claude-code": {"attempt": "none", "cause": "other"}, "codex": {"attempt": "none"}}},
    {**HANDOFF, "other_omlx": "kept_both"},
])
def test_handoff_refuses_missing_tool_or_unsafe_failure_shape(client, handoff):
    token = _create_account(client)

    assert _put(client, {"handoff": handoff}, token).status_code == 422
    assert "handoff" not in (setup_progress.read() or {})


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


ALL_PICKS = {"claude": True, "chatgpt": False, "key": True}
HANDOFF = {
    "tools": {
        "claude-code": {"attempt": "install_failed", "cause": "download_unavailable", "ref": "setup-42"},
        "codex": {"attempt": "none"},
    },
}


def test_welcome_picks_survive_the_account_and_a_reload(client):
    assert _put(client, {"picks": ALL_PICKS, "local_ai": True}).status_code == 200  # checked on Welcome, before any account
    token = _create_account(client)

    for _reload in range(2):
        progress = _progress(client, token)
        assert progress["picks"] == ALL_PICKS
        assert progress["local_ai"] is True
        assert progress["step"] == "models"


def test_picks_change_after_the_account_and_a_later_write_keeps_the_rest(client):
    token = _create_account(client)
    assert _put(client, {"picks": ALL_PICKS}, token).status_code == 200
    assert _put(client, {"picks": {"claude": False, "chatgpt": True, "key": False}}, token).status_code == 200
    assert _put(client, {"local_ai": False}, token).status_code == 200

    progress = _progress(client, token)
    assert progress["picks"] == {"claude": False, "chatgpt": True, "key": False}
    assert progress["local_ai"] is False


def test_no_picks_reads_as_none_not_as_all_unchecked(client):
    assert _progress(client, _create_account(client))["picks"] is None


@pytest.mark.parametrize("picks", [
    {"claude": True},
    {"claude": True, "chatgpt": True, "key": True, "other": True},
    {"claude": "yes", "chatgpt": False, "key": False},
])
def test_picks_must_say_yes_or_no_for_exactly_the_three_choices(client, picks):
    token = _create_account(client)

    assert _put(client, {"picks": picks}, token).status_code == 422
    assert _progress(client, token)["picks"] is None


def test_a_completed_setup_ignores_a_late_picks_write(client):
    token = _create_account(client)
    assert _put(client, {"picks": ALL_PICKS}, token).status_code == 200
    assert _put(client, {"completed": True}, token).status_code == 200
    assert _put(client, {"picks": {"claude": False, "chatgpt": False, "key": False}}, token).status_code == 200

    assert _progress(client, token)["picks"] == ALL_PICKS
