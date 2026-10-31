"""A usable added account runs while another account of the same vendor is limited.

Pins, for Claude Code and Codex alike: the unscoped catalog keeps the workspace
Default's meaning; a dispatch naming an account is decided on that account in
the actor's project; and a head held on a limit moves once its rollover
destination becomes usable, with nothing else re-offering it."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connections import account_moves, account_rollover, harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import chosen_account_id, select_account, set_policy
from connections.account_receipts import capture_receipt
from connections.models import upsert_connection
from connections.vendor_accounts import add_account, list_accounts, set_account_state
from vendor_account_fixtures import accounts, default_receipt, signed_in_default  # noqa: F401

VENDORS = ("codex", "claude-code")
LIMIT = dict(reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00", source="rate_limit_update")


def _selected(row):
    return next(item for item in row["runs_on"] if item["selected"])


def _vendor(db, provider):
    """Two signed-in added accounts for ``provider``, its test model, and a project thread on it."""
    model, thread = f"{provider}/test", f"{provider}-held"
    if provider != "codex":
        upsert_connection(type="cli", provider=provider, status="available", config={"binary": "/bin/synthetic"})
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
                   "VALUES (?,'Test',?,?,'test',?,'t','t')", (model, model, provider, provider))
    db.execute("UPDATE models SET context_window=200000 WHERE id=?", (model,))
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES (?,'user',?,'destination-project','t','t')", (thread, model))
    db.commit()
    signed_in_default(provider, db=db, models=(model,))  # the workspace Default, an account like the others
    first, second = (set_account_state(add_account(provider, name)["id"], "signed_in", expected_revision=0)
                     for name in ("First", "Second"))
    if provider == 'codex':
        for account in (first, second):
            (Path(account['source_home']) / 'auth.json').write_text(json.dumps({
                'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'},
            }))
    return SimpleNamespace(provider=provider, model=model, thread=thread, first=first, second=second)


def _discover(db, provider, account, model):
    receipt = capture_receipt(provider, account_id=account["id"], db=db)
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models={model: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()
    return receipt


@pytest.mark.parametrize("provider", VENDORS)
def test_dispatch_naming_a_usable_account_runs_while_the_default_is_limited(accounts, provider):  # noqa: F811
    from llm.delegation_model import eligible_delegation_model
    from llm.model_status import get_model_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    _discover(db, provider, v.first, v.model)
    harness_usage.record_allowed(provider=provider, source="discovery", db=db,
                                 receipt=capture_receipt(provider, account_id=v.first["id"], db=db))
    harness_usage.record_limited(provider=provider, receipt=default_receipt(provider, db=db), **LIMIT)

    # Unscoped keeps the workspace Default's meaning: limited, with its reset.
    row = _selected({r["id"]: r for r in get_model_status_rows()}[v.model])
    assert (row["status"], row["status_reason"], row["resets_at"]) == (
        "unavailable", "usage_limited", LIMIT["resets_at"])
    _, refusal = eligible_delegation_model(v.model)
    assert "usage_limited" in refusal

    assert eligible_delegation_model(v.model, account_id=v.first["id"], project_id="actor-project") == (v.model, None)

    set_account_state(v.second["id"], "signed_out", expected_revision=v.second["revision"])
    _, refusal = eligible_delegation_model(v.model, account_id=v.second["id"], project_id="actor-project")
    assert "'Second'" in refusal and v.second["id"] in refusal and "account_signed_out" in refusal

    set_policy("actor-project", provider, rollover=False, order=None,
               disabled=[account["id"] for account in list_accounts(provider=provider, db=db) if account["id"] != v.second["id"]],
               expected_revision=0, db=db)
    _, refusal = eligible_delegation_model(v.model, account_id=v.first["id"], project_id="actor-project")
    assert "'First'" in refusal and "account_project_denied" in refusal


@pytest.mark.parametrize("provider", VENDORS)
def test_dispatch_naming_no_account_reads_the_account_its_child_binds(accounts, provider):  # noqa: F811
    from llm.delegation_model import dispatch_scope, eligible_delegation_model

    db = accounts[0]
    v = _vendor(db, provider)
    _discover(db, provider, v.first, v.model)
    harness_usage.record_allowed(provider=provider, source="discovery", db=db,
                                 receipt=capture_receipt(provider, account_id=v.first["id"], db=db))
    harness_usage.record_limited(provider=provider, receipt=default_receipt(provider, db=db), **LIMIT)

    # The project defaults to a usable added account: admitted while the Default is limited.
    set_policy("actor-project", provider, rollover=False, order=[v.first["id"]], disabled=[],
               expected_revision=0, db=db)
    assert eligible_delegation_model(v.model, **dispatch_scope({}, "actor-project")) == (v.model, None)

    # With the Default disabled, the child selects its enabled sibling and reports that sibling's readiness.
    harness_usage.record_allowed(provider=provider, source="discovery", db=db, receipt=default_receipt(provider, db=db))
    assert eligible_delegation_model(v.model) == (v.model, None)
    set_policy("actor-project", provider, rollover=False, order=None,
               disabled=[account["id"] for account in list_accounts(provider=provider, db=db) if account["id"] != v.second["id"]],
               expected_revision=1, db=db)
    _, refusal = eligible_delegation_model(v.model, **dispatch_scope({}, "actor-project"))
    assert "account_model_not_discovered" in refusal


@pytest.mark.parametrize("provider", VENDORS)
def test_thread_scoped_catalog_reads_the_threads_usable_account(accounts, provider):  # noqa: F811
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    _discover(db, provider, v.first, v.model)
    harness_usage.record_limited(provider=provider, receipt=default_receipt(provider, db=db), **LIMIT)
    select_account(v.thread, provider, account_id=v.first["id"], db=db)

    row = _selected({r["id"]: r for r in scoped_status_rows(thread_id=v.thread)}[v.model])
    assert (row["status"], row["account_id"]) == ("ready", v.first["id"])
    harness_usage.record_limited(provider=provider, db=db,
                                 receipt=capture_receipt(provider, account_id=v.first["id"], db=db), **LIMIT)
    row = _selected({r["id"]: r for r in scoped_status_rows(thread_id=v.thread)}[v.model])
    assert (row["status"], row["status_reason"], row["resets_at"]) == (
        "unavailable", "usage_limited", LIMIT["resets_at"])


def _limited_rollover_policy(db, provider, v, *, switch_on_limit=True):
    from connections.vendor_accounts import default_account

    _discover(db, provider, v.first, v.model)
    harness_usage.record_allowed(provider=provider, source="discovery", db=db,
                                 receipt=capture_receipt(provider, account_id=v.first["id"], db=db))
    default = default_account(provider, db=db)
    harness_usage.record_limited(provider=provider, receipt=default_receipt(provider, db=db), **LIMIT)
    set_policy("destination-project", provider, rollover=switch_on_limit,
               order=[default["id"], v.first["id"]], disabled=[v.second["id"]], expected_revision=0, db=db)
    return default


@pytest.mark.parametrize("provider", VENDORS)
def test_picker_rolls_limited_default_to_next_usable_policy_account(accounts, provider):  # noqa: F811
    from connections.account_selection import bind_model_account
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    default = _limited_rollover_policy(db, provider, v)

    row = _selected({item["id"]: item for item in scoped_status_rows(thread_id=v.thread)}[v.model])
    assert (row["status"], row["account_id"]) == ("ready", v.first["id"])
    assert bind_model_account(v.thread, v.model, db=db) == v.first["id"]
    binding = db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                         (v.thread, provider)).fetchone()
    assert binding["account_id"] == v.first["id"] != default["id"]


@pytest.mark.parametrize("provider", VENDORS)
def test_explicit_account_does_not_roll_over(accounts, provider):  # noqa: F811
    from connections.account_selection import bind_model_account
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    default = _limited_rollover_policy(db, provider, v)

    row = _selected({item["id"]: item for item in scoped_status_rows(thread_id=v.thread, account_id=default["id"])}[v.model])
    assert (row["status"], row["status_reason"], row["account_id"]) == (
        "unavailable", "usage_limited", default["id"])
    assert bind_model_account(v.thread, v.model, account_id=default["id"], db=db) == default["id"]


@pytest.mark.parametrize("provider", VENDORS)
def test_picker_rebinds_limited_existing_idle_policy_binding(accounts, provider):  # noqa: F811
    from connections.account_policy import select_account
    from connections.account_selection import bind_model_account
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    default = _limited_rollover_policy(db, provider, v)
    assert select_account(v.thread, provider, db=db) == default["id"]

    row = _selected({item["id"]: item for item in scoped_status_rows(thread_id=v.thread)}[v.model])
    assert (row["status"], row["account_id"]) == ("ready", v.first["id"])
    assert bind_model_account(v.thread, v.model, db=db) == v.first["id"]
    binding = db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                         (v.thread, provider)).fetchone()
    epoch = db.execute("SELECT runtime_context_epoch FROM threads WHERE id=?", (v.thread,)).fetchone()[0]
    assert (binding["account_id"], epoch) == (v.first["id"], 1)


@pytest.mark.parametrize("provider", VENDORS)
def test_picker_reads_the_next_turns_account_but_a_running_turn_keeps_its_binding(accounts, provider):  # noqa: F811
    from connections.account_policy import select_account
    from connections.account_selection import bind_model_account
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    default = _limited_rollover_policy(db, provider, v)
    assert select_account(v.thread, provider, db=db) == default["id"]
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES ('busy',?,?,?,'running','t','b')", (v.thread, 0, v.model))
    db.commit()

    row = _selected({item["id"]: item for item in scoped_status_rows(thread_id=v.thread)}[v.model])
    # The next turn is claimed after this one and moves; a running turn keeps its account until then.
    assert (row["status"], row["account_id"]) == ("ready", v.first["id"])
    assert bind_model_account(v.thread, v.model, db=db) == default["id"]


@pytest.mark.parametrize("provider", VENDORS)
def test_picker_does_not_roll_over_when_policy_is_off(accounts, provider):  # noqa: F811
    from connections.account_selection import bind_model_account
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    default = _limited_rollover_policy(db, provider, v, switch_on_limit=False)

    row = _selected({item["id"]: item for item in scoped_status_rows(thread_id=v.thread)}[v.model])
    assert (row["status"], row["status_reason"], row["account_id"]) == (
        "unavailable", "usage_limited", default["id"])
    assert bind_model_account(v.thread, v.model, db=db) == default["id"]



@pytest.mark.parametrize("provider", VENDORS)
def test_rollover_claim_without_opt_in_never_reads_an_unbound_vendor_account(accounts, monkeypatch, provider):  # noqa: F811
    from infra.input_admission import accept_thread_input

    db = accounts[0]
    if provider == "codex":
        model, thread = "codex/test", "destination"
    else:
        v = _vendor(db, provider)
        model, thread = v.model, v.thread
    db.execute("DELETE FROM vendor_accounts WHERE connection_id IN (SELECT id FROM connections WHERE provider=?)", (provider,))
    db.commit()
    accept_thread_input(thread, role="user", content="input", intent="apply_now", message_id="input", db=db)
    head = db.execute("SELECT id FROM messages WHERE id='input'").fetchone()
    reads = []

    def unexpected(account_id, *, db):
        reads.append(account_id)
        raise AssertionError("rollover must not read an account before opt-in")

    monkeypatch.setattr(account_rollover, "get_account", unexpected)
    assert account_rollover.commit_rollover(account_rollover.plan_rollover(thread, eligible_rows=[head], db=db), db=db) is None
    assert reads == []


def _hold(db, monkeypatch, v):
    """Hold the thread's head on its limited source, First, with rollover onto Second switched on."""
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    set_policy("destination-project", v.provider, rollover=True,
               order=[v.first["id"], v.second["id"]], disabled=[], expected_revision=0, db=db)
    select_account(v.thread, v.provider, db=db)
    harness_usage.record_limited(provider=v.provider, db=db,
                                 receipt=capture_receipt(v.provider, account_id=v.first["id"], db=db), **LIMIT)
    monkeypatch.setattr(account_moves, "_publish", lambda _event: None)
    accept_thread_input(v.thread, role="user", content="input", intent="apply_now", message_id="input", db=db)
    held = start_idle_turn(v.thread, turn_id="held", boot_epoch="test", started_at="t", db=db)
    assert type(held).__name__ == "HeldAdmission"


async def _held_then_destination_usable(db, monkeypatch, v, make_usable):
    """Hold a head on the limited source while the destination is not yet usable, then make it usable."""
    import infra.app_loop as app_loop

    _hold(db, monkeypatch, v)
    admitted = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_turn_admission", admitted)
    app_loop.capture(asyncio.get_running_loop())
    try:
        make_usable()
        for _ in range(3000):  # Up to 30s on a loaded runner; exits at the first admission.
            if admitted.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()
    return admitted


@pytest.mark.parametrize("provider", VENDORS)
async def test_a_held_head_moves_when_its_destination_reads_allowed(accounts, monkeypatch, provider):  # noqa: F811
    db = accounts[0]
    v = _vendor(db, provider)
    _discover(db, provider, v.first, v.model)
    receipt = _discover(db, provider, v.second, v.model)  # Discovered, usage not yet known.

    admitted = await _held_then_destination_usable(db, monkeypatch, v, lambda: harness_usage.record_allowed(
        provider=provider, source="rate_limit_update", receipt=receipt, db=db))

    batch = admitted.await_args.args[0]
    assert type(batch).__name__ == "ClaimedBatch" and [item.message_id for item in batch.items] == ["input"]
    assert chosen_account_id(v.thread, provider, db=db) == v.second["id"]


@pytest.mark.parametrize("provider", VENDORS)
async def test_a_held_head_moves_when_its_destination_finishes_discovery(accounts, monkeypatch, provider):  # noqa: F811
    from connections.account_discovery_commit import commit_discovery_bundle

    db = accounts[0]
    v = _vendor(db, provider)
    _discover(db, provider, v.first, v.model)
    receipt = capture_receipt(provider, account_id=v.second["id"], db=db)
    harness_usage.record_allowed(provider=provider, source="rate_limit_update", receipt=receipt, db=db)

    admitted = await _held_then_destination_usable(db, monkeypatch, v, lambda: commit_discovery_bundle(
        provider, SimpleNamespace(usage_report=None, cache_stage=None), [{"selector_id": "test", "available": True}],
        context=SimpleNamespace(receipt=receipt, account_id=v.second["id"]), harness_version="test", db=db))

    batch = admitted.await_args.args[0]
    assert type(batch).__name__ == "ClaimedBatch"
    assert chosen_account_id(v.thread, provider, db=db) == v.second["id"]


@pytest.mark.parametrize("provider", VENDORS)
def test_a_discovery_commit_offers_held_heads_once(accounts, monkeypatch, provider):  # noqa: F811
    import infra.connection_limit_drive as drive
    from infra.database import write_transaction

    db = accounts[0]
    v = _vendor(db, provider)
    _discover(db, provider, v.first, v.model)
    receipt = _discover(db, provider, v.second, v.model)
    _hold(db, monkeypatch, v)
    offers = []
    monkeypatch.setattr(drive, "schedule_limited_heads", offers.append)

    # As commit_discovery_bundle does: the usage read turns allowed, then the bundle offers.
    with write_transaction(db, scope="test_discovery_bundle") as connection:
        harness_usage.record_allowed(provider=provider, source="rate_limit_update", receipt=receipt, db=connection)
        account_rollover.offer_destination(provider, v.second["id"], db=connection)

    assert offers == [[v.thread]]


def _default_login(db, account, login):
    db.execute("UPDATE vendor_accounts SET identity_json=? WHERE id=?", (json.dumps({"default_comparison": {
        "facts": {"account_fp": "synthetic"}, "auth_state": "known", "observation_seq": login, "login_seq": login}}),
        account["id"]))
    db.commit()


@pytest.mark.parametrize("provider", VENDORS)
def test_a_fenced_default_discovery_keeps_the_vendor_rows(accounts, provider):  # noqa: F811
    from connections.account_evidence import readiness_reason
    from connections.vendor_accounts import default_account
    from llm.model_status import get_model_status_rows

    db = accounts[0]
    v = _vendor(db, provider)
    _default_login(db, default_account(provider, db=db), 1)
    _discover(db, provider, default_account(provider, db=db), v.model)
    before = _selected({r["id"]: r for r in get_model_status_rows()}[v.model])
    assert before["status"] == "ready"

    _default_login(db, default_account(provider, db=db), 2)  # A new login fences the published list.
    assert readiness_reason(default_account(provider, db=db), model=v.model) is None
    after = _selected({r["id"]: r for r in get_model_status_rows()}[v.model])
    assert (after["status"], after.get("status_reason")) == (before["status"], before.get("status_reason"))
