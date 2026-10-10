"""Renaming an account is a label change: no login, model evidence or limit attribution moves."""
from __future__ import annotations

import pytest

from tests.test_vendor_account_policy import accounts  # noqa: F401

MODEL = "codex/test"


def _row():
    """One vendor catalog row as the authenticated catalog sees it."""
    return {"connection": "codex", "litellm_model": MODEL, "status": "ready", "context_window": 123456,
            "capabilities": {"reasoning_config": {"default": "high"}}, "resolved_identity": "vendor-only",
            "usage": {"usable": True, "plan": "plan-one"}, "cache_marker": "Cached"}


def _account(db, account_id):
    from connections.vendor_accounts import get_account

    return get_account(account_id, db=db)


def _reason(db, account_id, model=MODEL):
    from connections.account_evidence import readiness_reason

    return readiness_reason(_account(db, account_id), model=model)


def _publish(db, account, model=MODEL):
    """The account's own discovery lists ``model``, as a real refresh would leave it."""
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt("codex", account_id=account["id"], db=db)
    assert receipt is not None
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models={model: {"available": True, "context_window": 123456,
                                          "resolved_identity": "account-one",
                                          "capabilities": {"tools": True}}}, db=db)
    db.commit()
    return receipt


def test_a_rename_cannot_hide_the_default_vendors_catalog_rows(accounts):
    """The Default's models are the vendor catalog's, so a label edit cannot fence them."""
    from connections.vendor_accounts import record_default_identity, rename_account
    from llm.account_catalog import project_accounts

    db, legacy, _, _ = accounts
    record_default_identity("codex", {"account_fp": "fp-one"}, db=db)
    db.commit()
    _publish(db, legacy)
    assert _reason(db, legacy["id"]) is None

    rename_account(legacy["id"], "Renamed", expected_revision=legacy["revision"], db=db)
    db.commit()

    assert _reason(db, legacy["id"]) is None
    raw = project_accounts([_row()], thread_id="destination", db=db)[0]
    selected = next(item for item in raw["runs_on"] if item["selected"])
    assert selected["status"] == "ready" and selected["status_reason"] is None


def test_the_default_needs_no_published_record_of_its_own(accounts):
    """Observing the Default invalidates its own record; its rows stay selectable regardless."""
    from connections.account_evidence import invalidate_discovery
    from connections.vendor_accounts import record_default_identity, rename_account

    db, legacy, _, _ = accounts
    record_default_identity("codex", {"account_fp": "fp-one"}, db=db)
    db.commit()
    _publish(db, legacy)
    invalidate_discovery(_account(db, legacy["id"]), login_seq=0, auth_state="known", db=db)
    db.commit()
    assert _reason(db, legacy["id"]) is None

    rename_account(legacy["id"], "Renamed", expected_revision=legacy["revision"], db=db)
    db.commit()
    assert _reason(db, legacy["id"]) is None


def test_per_account_discovery_never_gates_the_default_vendors_rows(accounts):
    """The Default's models are the vendor catalog's: neither a failed probe nor a
    model list that lacks the row may hide them, while an added account keeps both holds."""
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import record_default_identity

    db, legacy, second, _ = accounts
    record_default_identity("codex", {"account_fp": "fp-one"}, db=db)
    db.commit()
    for account in (_account(db, legacy["id"]), _account(db, second["id"])):
        receipt = capture_receipt("codex", account_id=account["id"], db=db)
        assert mark_discovery(account["id"], receipt=receipt, status="failed", cause="probe_failed",
                              models={"codex/other": {"available": True}}, db=db)
    db.commit()

    assert _reason(db, legacy["id"]) is None
    assert _reason(db, second["id"]) == "account_model_not_discovered"


def test_an_ambient_default_stays_ready_when_its_record_goes_stale(accounts):
    """A stale per-account record holds an added account and cannot hide the Default's rows.

    The same stamp bump ages both accounts' captured receipts; only the account that owns
    a discovery record loses its rows.
    """
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import record_default_identity
    from llm.account_catalog import project_accounts

    db, legacy, second, _ = accounts
    record_default_identity("codex", {"account_fp": "fp-one"}, db=db)
    db.commit()
    for account in (_account(db, legacy["id"]), _account(db, second["id"])):
        receipt = capture_receipt("codex", account_id=account["id"], db=db)
        assert mark_discovery(account["id"], receipt=receipt, status="ready",
                              models={MODEL: {"available": True}}, db=db)
    db.commit()
    assert _reason(db, legacy["id"]) is None and _reason(db, second["id"]) is None

    for account_id in (legacy["id"], second["id"]):
        db.execute("UPDATE vendor_accounts SET revision=revision+1,"
                   "discovery_json=json_set(discovery_json,'$.login_revision',revision+1) WHERE id=?",
                   (account_id,))
    db.commit()

    assert _reason(db, legacy["id"]) is None
    assert _reason(db, second["id"]) == "account_model_not_discovered"
    raw = project_accounts([_row()], thread_id="destination", db=db)[0]
    selected = next(item for item in raw["runs_on"] if item["selected"])
    assert selected["account_id"] == legacy["id"]
    assert selected["status"] == "ready" and selected["status_reason"] is None


def test_a_rename_cannot_hide_an_added_accounts_models(accounts):
    """A rename ages nothing: the account's own discovery still names its models."""
    from connections.account_policy import select_account
    from vendor_account_fixtures import set_legacy_policy
    from connections.vendor_accounts import rename_account
    from llm.account_catalog import project_accounts

    db, _, second, _ = accounts
    assert _account(db, second["id"])["source_home"] is not None
    _publish(db, second)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]],
                      default_account_id=second["id"], db=db)
    assert select_account("destination", "codex", db=db) == second["id"]
    assert _reason(db, second["id"]) is None

    rename_account(second["id"], "Renamed", expected_revision=second["revision"], db=db)
    db.commit()

    assert _reason(db, second["id"]) is None
    raw = project_accounts([_row()], thread_id="destination", db=db)[0]
    selected = next(item for item in raw["runs_on"] if item["selected"])
    assert selected["account_id"] == second["id"]
    assert selected["status"] == "ready" and selected["status_reason"] is None
    assert raw["resolved_identity"] == "account-one"


@pytest.mark.parametrize("which", ["default", "added"])
def test_a_rename_does_not_age_a_captured_login_receipt(accounts, which):
    from connections.account_receipts import capture_receipt, receipt_is_current
    from connections.vendor_accounts import record_default_identity, rename_account

    db, legacy, second, _ = accounts
    if which == "default":
        record_default_identity("codex", {"account_fp": "fp-one"}, db=db)
        db.commit()
        account, revision = _account(db, legacy["id"]), legacy["revision"]
    else:
        _publish(db, second)
        account, revision = second, _account(db, second["id"])["revision"]
    captured = capture_receipt("codex", account_id=account["id"], db=db)
    assert captured is not None and receipt_is_current(captured, db=db)

    rename_account(account["id"], "Renamed", expected_revision=revision, db=db)
    db.commit()

    assert receipt_is_current(captured, db=db)


def test_another_accounts_stale_receipt_still_refuses_a_default_launch(accounts):
    """The login-0 bypass belongs to this row's own receipt, not to any captured one."""
    from connections.account_policy import AccountAdmissionError, require_launch
    from connections.account_receipts import capture_receipt, receipt_is_current
    from connections.account_runtime import LaunchHomes
    from connections.vendor_accounts import set_account_state

    db, legacy, second, _ = accounts
    foreign = capture_receipt("codex", account_id=second["id"], db=db)
    set_account_state(second["id"], "signed_in", expected_revision=_account(db, second["id"])["revision"], db=db)
    db.commit()
    assert not receipt_is_current(foreign, db=db)

    with pytest.raises(AccountAdmissionError) as refused:
        require_launch("codex", account_id=legacy["id"], frozen=True, model=MODEL,
                       launch=LaunchHomes(legacy["id"], False), receipt=foreign, db=db)
    assert refused.value.reason == "account_login_unverified"


def test_a_rename_keeps_the_usage_and_limit_attribution_of_a_captured_login(accounts):
    from connections.account_receipts import capture_receipt, subject_is_current, write_account
    from connections.usage_verdicts import LimitSubject
    from connections.vendor_accounts import rename_account

    db, _, second, _ = accounts
    captured = capture_receipt("codex", account_id=second["id"], db=db)
    subject = LimitSubject("codex", "account:" + second["id"], second["id"], captured)
    rename_account(second["id"], "Renamed", expected_revision=second["revision"], db=db)
    db.commit()

    assert write_account("codex", captured, db=db) == second["id"]
    assert subject_is_current(subject, db=db)


@pytest.mark.parametrize("state", ["signed_out", "removed"])
def test_a_lifecycle_change_still_holds_an_added_accounts_models(accounts, state):
    from connections.account_policy import account_reason
    from connections.vendor_accounts import set_account_state

    db, _, second, _ = accounts
    _publish(db, second)
    assert _reason(db, second["id"]) is None

    set_account_state(second["id"], state, expected_revision=_account(db, second["id"])["revision"], db=db)
    db.commit()

    assert _reason(db, second["id"]) == "account_model_not_discovered"
    assert account_reason(_account(db, second["id"]), provider="codex", project_ids=["destination-project"],
                          execution=True, model=MODEL, db=db) in (f"account_{state}",)


def test_a_second_login_still_ages_the_first_login_evidence(accounts):
    from connections.account_receipts import capture_receipt, receipt_is_current
    from connections.vendor_accounts import rename_account, set_account_state

    db, _, second, _ = accounts
    _publish(db, second)
    captured = capture_receipt("codex", account_id=second["id"], db=db)
    rename_account(second["id"], "Renamed", expected_revision=_account(db, second["id"])["revision"], db=db)
    db.commit()
    assert receipt_is_current(captured, db=db)

    set_account_state(second["id"], "signed_in", expected_revision=_account(db, second["id"])["revision"], db=db)
    db.commit()

    assert not receipt_is_current(captured, db=db)
    assert _reason(db, second["id"]) == "account_model_not_discovered"


def test_a_genuinely_changed_default_login_still_refuses_the_old_turn(accounts):
    from connections.account_policy import AccountAdmissionError, require_launch
    from connections.account_receipts import capture_receipt
    from connections.account_runtime import LaunchHomes
    from connections.vendor_accounts import record_default_identity

    db, legacy, _, _ = accounts
    record_default_identity("codex", {"account_fp": "fp-one"}, db=db)
    db.commit()
    captured = capture_receipt("codex", account_id=legacy["id"], db=db)
    record_default_identity("codex", {"account_fp": "fp-two"}, db=db)
    db.commit()

    with pytest.raises(AccountAdmissionError) as refused:
        require_launch("codex", account_id=legacy["id"], frozen=True, model=MODEL,
                       launch=LaunchHomes(legacy["id"], False), receipt=captured, db=db)
    assert refused.value.reason == "account_login_unverified"


def test_an_added_account_never_discovered_still_needs_its_own_evidence(accounts):
    """The ambient bypass belongs to the built-in Default, not to any home-less row."""
    from connections.account_evidence import invalidate_discovery

    db, _, second, _ = accounts
    invalidate_discovery(_account(db, second["id"]), login_seq=0, auth_state="known", db=db)
    db.commit()

    assert _reason(db, second["id"]) == "account_model_not_discovered"


def test_an_unverified_default_login_still_refuses_its_rows(accounts):
    from connections.vendor_accounts import record_default_identity, rename_account

    db, legacy, _, _ = accounts
    record_default_identity("codex", {}, db=db)
    db.commit()
    assert _reason(db, legacy["id"]) == 'account_login_unverified'

    rename_account(legacy["id"], "Renamed", expected_revision=_account(db, legacy["id"])["revision"], db=db)
    db.commit()
    assert _reason(db, legacy["id"]) == 'account_login_unverified'
