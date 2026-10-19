"""The reviewer setting follows the reviewer's own per-scope account resolution.

The accounts, projects, policies and usage come from the real account tables;
only the HTTP client and the owner session are test doubles.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vendor_account_fixtures import accounts, receipt_of, set_legacy_policy  # noqa: F401

from connections import harness_usage
from connections.account_evidence import mark_discovery

MODEL = "codex/test"
PROJECTS = ("actor-project", "destination-project")
RESET = "2099-01-01T00:00:00+00:00"
LATER_RESET = "2099-02-01T00:00:00+00:00"
WORKSPACE = "agents outside a project"


def _client() -> TestClient:
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _until(reset: str = RESET) -> str:
    moment = datetime.fromisoformat(reset).astimezone()
    return f"{moment:%b} {moment.day}, {moment.hour % 12 or 12}:{moment:%M %p %Z}"


def _discover(db, account, models=(MODEL,)):
    mark_discovery(
        account["id"], receipt=receipt_of("codex", account["id"], db=db), status="ready",
        models={model: {"available": True, "resolved_identity": "synthetic"} for model in models},
        db=db,
    )
    db.commit()


def _limit(db, account, resets_at: str = RESET):
    harness_usage.record_limited(
        provider="codex", receipt=receipt_of("codex", account["id"], db=db),
        reason="usage_limit", resets_at=resets_at, source="rate_limit_update", db=db,
    )


def _allow(db, account):
    harness_usage.record_allowed(
        provider="codex", receipt=receipt_of("codex", account["id"], db=db),
        source="discovery", db=db,
    )


def _projects_use(db, *chosen, rollover=False):
    """Both projects pick the first account; only ``chosen`` are permitted."""
    for project in PROJECTS:
        set_legacy_policy(
            project, "codex", allowlist=[item["id"] for item in chosen],
            default_account_id=chosen[0]["id"], switch_on_limit=rollover, db=db,
        )


def _name() -> str:
    from api.system_reviewer import reviewer_row

    return str(reviewer_row(MODEL)["name"])


def _save(client: TestClient):
    return client.patch(
        "/api/settings",
        headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
        json={"host_access_reviewer_model": MODEL},
    )


def _status(client: TestClient) -> dict:
    return client.get(
        "/api/settings", headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()


def test_workspace_limited_but_projects_ready_saves_and_names_the_workspace(accounts):
    from infra.settings import get_agent_defaults

    db, legacy, second, third = accounts
    _discover(db, legacy)
    _discover(db, second)
    _projects_use(db, second)
    _limit(db, legacy)

    with _client() as client:
        saved = _save(client)
        status = _status(client)

    assert saved.status_code == 200
    assert get_agent_defaults()["host_access_reviewer_model"] == MODEL
    assert status["host_access_reviewer_status"] == "ready"
    assert status["host_access_reviewer_message"] == (
        f"{_name()} can't review requests from {WORKSPACE} right now "
        f"(usage limit until {_until()}). Those requests fail until then. "
        "Choose another model or turn on fallback."
    )


def test_archived_project_is_not_a_scope(accounts):
    from infra.settings import get_agent_defaults

    db, legacy, second, third = accounts
    _discover(db, legacy)
    _discover(db, second)
    _projects_use(db, second)
    _limit(db, legacy)
    for project in PROJECTS:
        db.execute("UPDATE projects SET status='archived' WHERE id=?", (project,))
    db.commit()

    with _client() as client:
        rejected = _save(client)

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == (
        f"{_name()} can't review requests right now (usage limit until {_until()}). "
        "Choose another model."
    )
    assert "host_access_reviewer_model" not in get_agent_defaults()


@pytest.mark.parametrize("stage", ["idea", "exploring", "validated"])
def test_unarchived_project_in_any_stage_is_a_scope(accounts, stage):
    db, legacy, second, third = accounts
    _discover(db, legacy)
    _discover(db, second)
    _projects_use(db, second)
    _limit(db, second)
    for project in PROJECTS:
        db.execute("UPDATE projects SET status=? WHERE id=?", (stage, project))
    db.commit()

    with _client() as client:
        saved = _save(client)
        status = _status(client)

    assert saved.status_code == 200
    assert status["host_access_reviewer_status"] == "ready"
    assert status["host_access_reviewer_message"] == (
        f"{_name()} can't review requests from {', '.join(PROJECTS)} right now "
        f"(usage limit until {_until()}). Those requests fail until then. "
        "Choose another model or turn on fallback."
    )


def test_project_rollover_to_a_ready_account_leaves_every_scope_runnable(accounts):
    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, account)
    _projects_use(db, second, third, rollover=True)
    _limit(db, second)
    _allow(db, third)

    with _client() as client:
        saved = _save(client)
        status = _status(client)

    assert saved.status_code == 200
    assert status["host_access_reviewer_status"] == "ready"
    assert status["host_access_reviewer_message"] is None


def test_rollover_with_every_destination_limited_shows_the_resolved_accounts_reset(accounts):
    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, account)
    _projects_use(db, second, third, rollover=True)
    _limit(db, legacy)
    _limit(db, second, RESET)
    _limit(db, third, LATER_RESET)

    with _client() as client:
        rejected = _save(client)

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == (
        f"{_name()} can't review requests right now (usage limit until {_until()}). "
        "Choose another model."
    )


def test_every_scope_limited_rejects_with_the_reset(accounts):
    from infra.settings import get_agent_defaults

    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, account)
    _projects_use(db, second)
    _limit(db, legacy)
    _limit(db, second)

    with _client() as client:
        rejected = _save(client)
        unsaved = "host_access_reviewer_model" not in get_agent_defaults()
        status = _status_after_selecting(client)

    lead = f"{_name()} can't review requests right now (usage limit until {_until()})."
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == f"{lead} Choose another model."
    assert unsaved
    assert status["host_access_reviewer_status"] == "unavailable"
    assert status["host_access_reviewer_message"] == (
        f"{lead} Host-access requests will fail until then. "
        "Choose another model or turn on fallback."
    )


def test_every_scope_limited_with_fallback_on_says_the_requesters_model_reviews(accounts):
    from infra.settings import save_agent_defaults

    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, account)
    _projects_use(db, second)
    _limit(db, legacy)
    _limit(db, second)
    save_agent_defaults({"host_access_reviewer_fallback": True})

    with _client() as client:
        status = _status_after_selecting(client)

    assert status["host_access_reviewer_status"] == "unavailable"
    assert status["host_access_reviewer_message"] == (
        f"{_name()} can't review requests right now (usage limit until {_until()}). "
        "Each requesting agent's own model is reviewing instead."
    )


def test_one_limited_scope_with_fallback_on_says_those_requests_use_the_requesters_model(accounts):
    from infra.settings import save_agent_defaults

    db, legacy, second, third = accounts
    _discover(db, legacy)
    _discover(db, second)
    _projects_use(db, second)
    _limit(db, legacy)
    save_agent_defaults({"host_access_reviewer_fallback": True})

    with _client() as client:
        status = _status_after_selecting(client)

    assert status["host_access_reviewer_status"] == "ready"
    assert status["host_access_reviewer_message"] == (
        f"{_name()} can't review requests from {WORKSPACE} right now "
        f"(usage limit until {_until()}). "
        "Those requests use the requesting agent's own model until then."
    )


def test_a_limited_scope_without_a_reset_time_does_not_say_until_then(accounts):
    db, legacy, second, third = accounts
    _discover(db, legacy)
    _discover(db, second, models=("codex/other",))
    _projects_use(db, second)

    with _client() as client:
        saved = _save(client)
        status = _status(client)

    assert saved.status_code == 200
    assert status["host_access_reviewer_status"] == "ready"
    assert status["host_access_reviewer_message"] == (
        f"{_name()} can't review requests from {', '.join(PROJECTS)} right now "
        "(the account doesn't offer this model). Those requests fail. "
        "Choose another model or turn on fallback."
    )


def _status_after_selecting(client: TestClient) -> dict:
    from infra.settings import save_agent_defaults

    save_agent_defaults({"host_access_reviewer_model": MODEL})
    return _status(client)


def test_selected_account_without_the_model_rejects_despite_a_ready_sibling(accounts):
    db, legacy, second, third = accounts
    _discover(db, legacy)
    _discover(db, second, models=("codex/other",))
    _discover(db, third)
    _projects_use(db, second, third)
    _limit(db, legacy)

    with _client() as client:
        rejected = _save(client)

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == (
        f"{_name()} can't review requests right now "
        f"(usage limit until {_until()}; the account doesn't offer this model). "
        "Choose another model."
    )


def test_model_unavailable_everywhere_rejects(accounts):
    db, legacy, second, third = accounts
    _discover(db, legacy)
    db.execute("UPDATE models SET available=0 WHERE id=?", (MODEL,))
    db.commit()

    with _client() as client:
        rejected = _save(client)

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == (
        f"{_name()} can't review requests right now "
        "(the account doesn't offer this model). Choose another model."
    )


def _native(db, *, enabled: int):
    db.execute(
        "INSERT INTO models(id,harness,provider,provider_model_id,litellm_model,"
        "display_name,backend,source,enabled,available,context_window,created_at,updated_at) "
        "VALUES ('omlx/native','native','omlx','native','omlx/native','Native',"
        "'network','test',?,1,32768,'t','t')",
        (enabled,),
    )
    db.commit()


def _save_native(client: TestClient):
    return client.patch(
        "/api/settings",
        headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
        json={"host_access_reviewer_model": "omlx/native"},
    )


def test_non_harness_model_needs_no_account_and_follows_the_model_check(accounts):
    db, legacy, second, third = accounts
    _native(db, enabled=1)
    _limit(db, legacy)

    with _client() as client:
        saved = _save_native(client)

    assert saved.status_code == 200


def test_withdrawn_non_harness_model_is_rejected(accounts):
    db, *_ = accounts
    _native(db, enabled=0)

    with _client() as client:
        rejected = _save_native(client)

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == (
        "That model isn't in the catalog. Choose another model."
    )
