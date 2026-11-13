"""Host-access reviews follow the requester's account, including pending moves."""

from types import SimpleNamespace

import pytest

MODEL = "claude-code/claude-opus-4-8"
PROJECT = "corsia-project"
_RESET = "2099-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reviewer_catalog(isolated_db):
    """The one Claude Code model the requester's account must offer."""
    import json

    from infra.database import get_db

    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO models "
            "(id,harness,provider,provider_model_id,litellm_model,display_name,"
            "backend,source,enabled,available,context_window,metadata_json,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL, "claude-code", "claude-code", "claude-opus-4-8", MODEL,
             "claude-opus-4-8", "cc", "test", 1, 1, 200_000,
             json.dumps({"resolved_identity": "claude-opus-4-8", "internal_reviewer": False}),
             "t", "t"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO connections "
            "(id,type,provider,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("reviewer-cc", "cli", "claude-code", "available", "t", "t"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """The project, a Default and a signed-in second Claude account, and a writable db."""
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, set_account_state
    from infra.database import get_db
    from vendor_account_fixtures import seed_default_account

    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path)
    (tmp_path / "projects" / "corsia").mkdir(parents=True)
    db = get_db()
    db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
               (PROJECT, "corsia", str(tmp_path / "projects" / "corsia")))
    default = seed_default_account("claude-code", db=db)
    default = set_account_state(default["id"], "signed_in", expected_revision=0, db=db)
    receipt = capture_receipt("claude-code", account_id=default["id"], db=db)
    assert mark_discovery(default["id"], receipt=receipt, status="ready",
                          models={MODEL: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    second = add_account("claude-code", "Second", db=db)
    second = set_account_state(second["id"], "signed_in", expected_revision=0, db=db)
    receipt = capture_receipt("claude-code", account_id=second["id"], db=db)
    assert mark_discovery(second["id"], receipt=receipt, status="ready",
                          models={MODEL: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()
    yield db, default, second
    db.close()


def _policy(db, *, allowlist, default_id, switch):
    from vendor_account_fixtures import set_legacy_policy

    set_legacy_policy(PROJECT, "claude-code", allowlist=allowlist, default_account_id=default_id,
                      switch_on_limit=switch, db=db)
    db.commit()


def _limit(db, account):
    from connections import harness_usage
    from connections.account_receipts import capture_receipt

    harness_usage.record_limited(
        provider="claude-code", receipt=capture_receipt("claude-code", account_id=account["id"], db=db),
        reason="usage_limit", resets_at=_RESET, source="rate_limit_update", db=db)
    db.commit()


def _allow(db, account):
    from connections import harness_usage
    from connections.account_receipts import capture_receipt

    harness_usage.record_allowed(
        provider="claude-code", receipt=capture_receipt("claude-code", account_id=account["id"], db=db),
        source="discovery", db=db)
    db.commit()


@pytest.mark.parametrize("binding", ["bound", "pending", "none", "signed_out", "bound_signed_out"])
def test_lease_review_uses_requester_turn_account(scope, binding):
    from host_access.errors import ReviewerUnavailableError
    from host_access.reviewer_target import _requester_reviewer_account

    db, default, second = scope
    _policy(db, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=False)
    db.execute(
        "INSERT INTO threads(id,user_id,title,model,project_id,created_at,updated_at) "
        "VALUES ('requester','user','requester',?,?,'t','t')", (MODEL, PROJECT),
    )
    if binding in {"bound", "pending"}:
        db.execute(
            "INSERT INTO thread_vendor_accounts(thread_id,provider,account_id,pending_account_id) "
            "VALUES ('requester','claude-code',?,?)",
            (second["id"] if binding == "bound" else default["id"],
             second["id"] if binding == "pending" else None),
        )
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (default["id"],))
    elif binding == "signed_out":
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (default["id"],))
    elif binding == "bound_signed_out":
        db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) "
                   "VALUES ('requester','claude-code',?)", (second["id"],))
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (second["id"],))
    db.commit()
    target = SimpleNamespace(harness="claude-code", catalog_id=MODEL, display_name=MODEL)

    if binding in {"signed_out", "bound_signed_out"}:
        with pytest.raises(ReviewerUnavailableError) as exc:
            _requester_reviewer_account(db, target, "requester")
        assert exc.value.outage.reason == "account_signed_out"
    else:
        expected = default["id"] if binding == "none" else second["id"]
        assert _requester_reviewer_account(db, target, "requester") == expected


def test_lease_review_uses_turn_rollover_once(scope):
    from host_access.reviewer_target import _requester_reviewer_account

    db, default, second = scope
    _policy(db, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)
    db.execute("INSERT INTO threads(id,user_id,title,model,project_id,created_at,updated_at) "
               "VALUES ('requester','user','requester',?,?,'t','t')", (MODEL, PROJECT))
    db.commit()
    _limit(db, default)
    _allow(db, second)
    target = SimpleNamespace(harness="claude-code", catalog_id=MODEL, display_name=MODEL)

    assert _requester_reviewer_account(db, target, "requester") == second["id"]


def test_pending_move_does_not_roll_over(scope):
    from host_access.errors import ReviewerUnavailableError
    from host_access.reviewer_target import _requester_reviewer_account

    db, default, second = scope
    _policy(db, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)
    db.execute("INSERT INTO threads(id,user_id,title,model,project_id,created_at,updated_at) "
               "VALUES ('requester','user','requester',?,?,'t','t')", (MODEL, PROJECT))
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id,pending_account_id) "
               "VALUES ('requester','claude-code',?,?)", (default["id"], second["id"]))
    db.commit()
    _allow(db, default)
    _limit(db, second)
    target = SimpleNamespace(harness="claude-code", catalog_id=MODEL, display_name=MODEL)

    with pytest.raises(ReviewerUnavailableError) as exc:
        _requester_reviewer_account(db, target, "requester")
    assert exc.value.outage.reason == "usage_limited"
