"""Host-access reviews follow the requester's account, including pending moves."""

from types import SimpleNamespace

import pytest

from test_reviewer_account_selection import (  # noqa: F401
    MODEL, PROJECT, _allow, _limit, _policy, _reviewer_catalog, scope,
)


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
