"""A model row says what the model is; an account's usage never becomes a row's status.

``runs_on`` carries what each permitted account can do with the model, whole, and the entry
marked ``selected`` is the one answer to which account the next turn runs on, so Settings (no
thread) and the picker (a thread) cannot disagree about the same model, and nothing downstream
derives that answer or its usage again.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connections import harness_usage
from connections.account_policy import select_account, turn_account
from vendor_account_fixtures import _ready, accounts, receipt_of, set_legacy_policy, vendors  # noqa: F401

LIMIT = dict(reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00", source="rate_limit_update")
USAGE_KEYS = ("usage", "usage_check", "usage_check_error", "status_resets_at", "accounts", "account_id")


def _spend_the_default(db, legacy, second):
    """Codex's Default is out of plan; an added account is signed in and usable."""
    db.execute("UPDATE models SET context_window=200000 WHERE id='codex/test'")
    _ready(db, second)
    harness_usage.record_allowed(provider="codex", source="discovery", db=db,
                                 receipt=receipt_of("codex", second["id"], db=db))
    harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", legacy["id"], db=db), **LIMIT, db=db)
    db.commit()


def _by_account(row):
    return {entry["account_id"]: entry for entry in row["runs_on"]}


@pytest.mark.asyncio
async def test_models_api_projects_a_selected_ready_account_as_runnable(accounts, monkeypatch):  # noqa: F811
    """``GET /api/models`` returns the shared scoped-picker row unchanged."""
    import api.model_account_scope as account_scope
    import api.models as models_api

    db, legacy, second, third = accounts
    _ready(db, second)
    select_account("destination", "codex", account_id=second["id"], db=db)
    monkeypatch.setattr(account_scope, "authorize_scope", lambda *_args, **_kwargs: True)

    payload = await models_api.list_models(request=object(), thread_id="destination")
    row = {item["id"]: item for item in payload["models"]}["codex/test"]

    assert next(item for item in row["runs_on"] if item["selected"])["status"] == "ready"
    assert row["runnable"] == {"status": "ready", "status_reason": None}


def test_a_limited_selected_account_is_runnable_when_another_account_is_ready(accounts):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db, legacy, second, third = accounts
    _ready(db, second)
    _ready(db, third)
    select_account("destination", "codex", account_id=second["id"], db=db)
    set_legacy_policy(None, "codex", allowlist=None, default_account_id=None, switch_on_limit=False, db=db)
    set_legacy_policy("destination-project", "codex", allowlist=None, default_account_id=None,
                      switch_on_limit=True, db=db)
    harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", second["id"], db=db), **LIMIT, db=db)
    db.commit()

    row = {item["id"]: item for item in scoped_status_rows(thread_id="destination")}["codex/test"]
    selected = next(item for item in row["runs_on"] if item["selected"])

    assert (selected["status"], selected["status_reason"], selected["resets_at"]) == (
        "unavailable", "usage_limited", LIMIT["resets_at"])
    assert row["runnable"] == {"status": "ready", "status_reason": None}


def test_a_limited_selected_account_is_runnable_when_rollover_is_off(accounts):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db, legacy, second, third = accounts
    _ready(db, second)
    _ready(db, third)
    select_account("destination", "codex", account_id=second["id"], db=db)
    set_legacy_policy("destination-project", "codex", allowlist=None, default_account_id=None,
                      switch_on_limit=False, db=db)
    harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", second["id"], db=db), **LIMIT, db=db)
    db.commit()

    row = {item["id"]: item for item in scoped_status_rows(thread_id="destination")}["codex/test"]
    selected = next(item for item in row["runs_on"] if item["selected"])

    assert (selected["status"], selected["status_reason"]) == ("unavailable", "usage_limited")
    assert row["runnable"] == {"status": "ready", "status_reason": None}


@pytest.mark.parametrize("rollover", [False, True])
def test_workspace_runnable_ignores_the_workspace_rollover_policy(accounts, rollover):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db, legacy, second, third = accounts
    _ready(db, second)
    _ready(db, third)
    set_legacy_policy(None, "codex", allowlist=None, default_account_id=None,
                      switch_on_limit=rollover, db=db)
    harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", second["id"], db=db), **LIMIT, db=db)
    db.commit()

    row = {item["id"]: item for item in scoped_status_rows(account_id=second["id"])}["codex/test"]
    selected = next(item for item in row["runs_on"] if item["selected"])

    assert (selected["status"], selected["status_reason"]) == ("unavailable", "usage_limited")
    assert row["runnable"] == {"status": "ready", "status_reason": None}


def test_a_limited_selected_account_is_unrunnable_when_every_account_is_limited(accounts):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db, legacy, second, third = accounts
    for account in (second, third):
        _ready(db, account)
    select_account("destination", "codex", account_id=second["id"], db=db)
    set_legacy_policy("destination-project", "codex", allowlist=None, default_account_id=None,
                      switch_on_limit=False, db=db)
    for account in (legacy, second, third):
        harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", account["id"], db=db), **LIMIT, db=db)
    db.commit()

    row = {item["id"]: item for item in scoped_status_rows(thread_id="destination")}["codex/test"]

    assert row["runnable"] == {"status": "unavailable", "status_reason": "usage_limited"}


def test_a_selected_account_without_the_model_is_not_runnable_with_a_ready_sibling(accounts):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db, legacy, second, third = accounts
    _ready(db, third)
    select_account("destination", "codex", account_id=second["id"], db=db)
    set_legacy_policy("destination-project", "codex", allowlist=None, default_account_id=None,
                      switch_on_limit=False, db=db)

    row = {item["id"]: item for item in scoped_status_rows(thread_id="destination")}["codex/test"]
    selected = next(item for item in row["runs_on"] if item["selected"])

    assert selected["status_reason"] == "account_model_not_discovered"
    assert row["runnable"] == {"status": "unavailable", "status_reason": "account_model_not_discovered"}


def test_non_harness_rows_do_not_project_runnable(accounts):  # noqa: F811
    from llm.account_catalog import project_accounts

    db, *_ = accounts
    row = project_accounts([{"connection": "omlx", "status": "ready", "status_reason": None}], db=db)[0]

    assert "runnable" not in row


def test_a_spent_default_does_not_make_the_unscoped_row_unavailable(accounts):  # noqa: F811
    from llm.model_status import get_model_status_rows

    db, legacy, second, third = accounts
    _spend_the_default(db, legacy, second)

    row = {item["id"]: item for item in get_model_status_rows()}["codex/test"]

    assert (row["status"], row.get("status_reason")) == ("ready", None)
    assert not [key for key in USAGE_KEYS if key in row]
    entries = _by_account(row)
    assert (entries[legacy["id"]]["status"], entries[legacy["id"]]["status_reason"],
            entries[legacy["id"]]["resets_at"]) == ("unavailable", "usage_limited", LIMIT["resets_at"])
    assert entries[second["id"]]["status"] == "ready"
    assert [entry["account_id"] for entry in row["runs_on"] if entry["selected"]] == [legacy["id"]]


def test_the_scoped_read_names_the_bound_account_and_every_permitted_one(accounts):  # noqa: F811
    from llm.model_catalog_projection import model_catalog_projection

    db, legacy, second, third = accounts
    _spend_the_default(db, legacy, second)
    select_account("destination", "codex", account_id=second["id"], db=db)

    payload = model_catalog_projection([], thread_id="destination")

    assert "selection" not in payload
    row = {item["id"]: item for item in payload["models"]}["codex/test"]
    assert set(_by_account(row)) == {legacy["id"], second["id"], third["id"]}
    assert [entry["account_id"] for entry in row["runs_on"] if entry["selected"]] == [second["id"]]
    assert row["status"] == "ready"


def test_the_workspace_read_marks_the_default_as_the_selected_account(accounts):  # noqa: F811
    from llm.model_catalog_projection import model_catalog_projection

    db, legacy, *_ = accounts
    db.execute("UPDATE models SET context_window=200000 WHERE id='codex/test'")
    db.commit()

    payload = model_catalog_projection([])
    row = {item["id"]: item for item in payload["models"]}["codex/test"]

    assert "selection" not in payload
    assert [entry["account_id"] for entry in row["runs_on"] if entry["selected"]] == [legacy["id"]]


def test_a_runs_on_entry_carries_the_whole_decision(accounts):  # noqa: F811
    """Status, reason, reset, the model's usage projection and the recheck state arrive together."""
    from connections.harness_usage import project_for_model, stored_usage
    from connections.vendor_accounts import get_account
    from llm.account_catalog import account_model_status
    from llm.model_status import get_model_status_rows

    db, legacy, second, third = accounts
    _spend_the_default(db, legacy, second)

    row = {item["id"]: item for item in get_model_status_rows()}["codex/test"]
    entries = _by_account(row)

    for account in (legacy, second):
        decision = account_model_status(get_account(account["id"], db=db), row, project_ids=[None], db=db)
        entry = entries[account["id"]]
        assert (entry["status"], entry["status_reason"], entry["usage"]) == (
            decision["status"], decision["status_reason"], decision["usage"])
        assert entry["usage"] == project_for_model(
            stored_usage(provider="codex", account_id=account["id"], db=db), model="codex/test", provider="codex")
    limited, usable = entries[legacy["id"]], entries[second["id"]]
    assert (limited["usage"]["usable"], limited["resets_at"]) == (False, LIMIT["resets_at"])
    assert (usable["usage"]["usable"], usable["resets_at"]) == (True, None)
    assert "usage_check" not in usable


def test_a_limited_entry_reports_the_recheck_the_picker_shows(accounts, monkeypatch):  # noqa: F811
    from llm.model_status import get_model_status_rows

    db, legacy, second, third = accounts
    _spend_the_default(db, legacy, second)
    monkeypatch.setattr("connections.harness_usage_check.is_checking", lambda account_id: account_id == legacy["id"])

    row = {item["id"]: item for item in get_model_status_rows()}["codex/test"]

    assert _by_account(row)[legacy["id"]]["usage_check"] == "checking"
    assert "usage_check" not in _by_account(row)[second["id"]]


@pytest.mark.asyncio
async def test_the_catalog_tool_reads_the_account_a_delegated_child_runs_on(accounts):  # noqa: F811
    """A parent bound to another account must not change which account's readiness the agent reads."""
    from llm.delegation_model import dispatch_scope, eligible_delegation_model
    from tools.model_catalog import ModelCatalogTool

    db, legacy, second, third = accounts
    db.execute("UPDATE models SET context_window=200000 WHERE id='codex/test'")
    for account in (legacy, second, third):
        _ready(db, account)
    set_legacy_policy("actor-project", "codex", allowlist=None, default_account_id=third["id"], db=db)
    select_account("actor", "codex", account_id=second["id"], db=db)  # the parent's own binding
    harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", third["id"], db=db), **LIMIT, db=db)
    db.commit()

    payload = json.loads(await ModelCatalogTool().execute(_thread_id="actor"))
    row = {item["id"]: item for item in payload["models"]}["codex/test"]
    (selected,) = [entry for entry in row["runs_on"] if entry["selected"]]
    model, refusal = eligible_delegation_model("codex/test", **dispatch_scope({}, "actor-project"))

    assert selected["account_id"] == third["id"]  # the project's default, not the parent's second
    assert (selected["status"], selected["status_reason"]) == ("unavailable", "usage_limited")
    assert (model, refusal is not None) == (None, True)
    assert "usage_limited" in refusal


@pytest.fixture
def client(accounts):  # noqa: F811
    from api.vendor_accounts import router
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "user"}
    with TestClient(app) as api:
        yield api


def test_vendor_accounts_carry_usage_and_a_label(vendors, client):  # noqa: F811
    from connections.vendor_accounts import record_default_identity

    db, added = vendors
    record_default_identity("claude-code", {"email": "me@example.com", "org_id": "org-1"}, db=db)
    db.commit()

    rows = client.get("/api/vendor-accounts", params={"provider": "claude-code"}).json()["accounts"]
    default = next(row for row in rows if row["is_default"])
    second = next(row for row in rows if not row["is_default"])

    assert default["label"] == {"name": "me@example.com", "email": "me@example.com",
                                "text": "me@example.com", "custom": None, "identified": True}
    assert second["label"] == {"name": second["name"], "email": None,
                               "text": second["name"], "custom": second["name"], "identified": True}
    for row in rows:
        assert {"state", "usable", "resets_at", "windows", "check", "allow_overage", "checking"} <= set(row["usage"])
        assert "default_comparison" not in str(row["identity"])


def test_account_usage_tags_every_window_with_its_model_scope(accounts):  # noqa: F811
    from datetime import datetime, timezone

    from connections.usage_projection import account_usage

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    usage = {"windows": {
        "a": {"kind": "seven_day", "used_percent": 10, "resets_at": "2026-01-02T00:00:00+00:00"},
        "b": {"kind": "seven_day_opus", "used_percent": 90, "resets_at": "2026-01-02T00:00:00+00:00"},
    }}

    shown = account_usage(usage, now=now)

    assert {window["kind"]: window["scope_model"] for window in shown["windows"]} == {
        "seven_day": None, "seven_day_opus": "opus"}
    assert shown["usable"] is True



def test_turn_account_moves_along_the_order_only_when_rollover_is_on_and_the_source_is_limited(accounts):  # noqa: F811
    db, legacy, second, third = accounts
    _spend_the_default(db, legacy, second)
    for rollover in (False, True):
        set_legacy_policy("destination-project", "codex", allowlist=[legacy["id"], second["id"]],
                   default_account_id=legacy["id"], switch_on_limit=rollover, db=db)
        started = turn_account("codex", thread_id="destination", db=db)
        moved = turn_account("codex", thread_id="destination", model="codex/test", db=db)
        assert started == (legacy["id"], None)
        assert moved == ((second["id"] if rollover else legacy["id"]), None)


def test_a_project_that_permits_no_account_reads_denied_for_the_model(accounts):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db, legacy, second, third = accounts
    db.execute("UPDATE models SET context_window=200000 WHERE id='codex/test'")
    set_legacy_policy("destination-project", "codex", allowlist=[], default_account_id=None, db=db)
    db.commit()

    row = {item["id"]: item for item in scoped_status_rows(project_id="destination-project")}["codex/test"]

    assert (row["status"], row["status_reason"], row["runs_on"]) == ("unavailable", "account_project_denied", [])
    assert row["runnable"] == {"status": "unavailable", "status_reason": "account_project_denied"}
    assert turn_account("codex", project_id="destination-project", db=db) == (
        legacy["id"], "account_project_denied")

    # Delegation reads the same projected row, so it refuses before launch.
    from llm.delegation_model import eligible_delegation_model

    selected, refusal = eligible_delegation_model("codex/test", project_id="destination-project")
    assert selected is None
    assert "account_project_denied" in refusal
