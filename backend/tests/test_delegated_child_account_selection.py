"""A delegated child must launch on the account its project's catalog projects.

``model_catalog`` and ``eligible_delegation_model`` ask ``scoped_turn_account`` *with the model*,
so the project's opt-in rollover decision is part of the answer, and the tool advertises the
``runs_on`` entry it marks ``selected`` as "the account a delegated turn in this project runs on".
Child creation asks the same owner with the model too (``bind_model_account`` passes it to
``select_account``), but that answer was reachable only when exactly one scope entry governed the
thread, and a spawned child always has two: its own project and its actor's. So the guard, not a
missing argument, blocked the projected account and the launch admitted the plain candidate
instead. The Turn-freeze boundary asks the owner without the model and reads the binding once one
exists. One distinct provider policy now answers that question, however many scopes named it; two
distinct policies still do not, even when they are written identically, because a move would need
both projects' switch and order and one project's preference must not silently win. These cases
drive the real owners only: the projected ``runs_on`` entry, the binding ``create_child_thread``
writes, the account the Turn freezes through ``turn_account_id``, and the detached reviewer's own
composition of the same owners.

Synthetic accounts only: no vendor call, login, secret or real account is touched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from connections import harness_usage
from connections.account_policy import AccountAdmissionError, chosen_account_id
from connections.account_receipts import capture_receipt
from connections.vendor_accounts import add_account, get_account, set_account_state
from vendor_account_fixtures import DISCOVERED, accounts, set_legacy_policy  # noqa: F401

PROJECT = "destination-project"
ACTOR_PROJECT = "actor-project"
RESET = "2099-01-01T00:00:00+00:00"


def _harness_row(provider: str, model: str) -> dict:
    return {"id": model, "litellm_model": model, "connection": provider, "status": "ready",
            "context_window": 200_000, "capabilities": {"own": True},
            "resolved_identity": "synthetic", "usage": {"usable": True}}


def _projected(db, provider: str, model: str) -> tuple[dict, dict]:
    """What the project's catalog says this scope's next turn of ``model`` runs on, and whether it can."""
    from llm.account_catalog import project_accounts

    row = project_accounts([_harness_row(provider, model)], project_id=PROJECT, db=db)[0]
    entry = next((item for item in row["runs_on"] if item["selected"]), None)
    if entry is None:
        entry = {"account_id": None, "status": row["status"], "status_reason": row.get("status_reason")}
    return entry, row["runnable"]


def _usable(db, provider: str, account, model: str) -> dict:
    """One signed-in account with a login, ready discovery for ``model`` and a recorded 'allowed'."""
    from connections.account_evidence import mark_discovery

    receipt = capture_receipt(provider, account_id=account["id"], db=db)
    assert receipt is not None, account["id"]
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models={model: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()  # usage records their own write scope; an ambient transaction here would nest
    harness_usage.record_allowed(provider=provider, receipt=receipt, source="discovery", db=db)
    db.commit()
    return get_account(account["id"], db=db)


def _limited(db, provider: str, account, model: str) -> None:
    receipt = capture_receipt(provider, account_id=account["id"], db=db)
    harness_usage.record_limited(provider=provider, receipt=receipt, reason="usage_limit",
                                 resets_at=RESET, source="rate_limit_update", db=db)
    db.commit()


def _sign_out(db, account) -> None:
    current = get_account(account["id"], db=db)
    set_account_state(account["id"], "signed_out", expected_revision=current["revision"], db=db)
    db.commit()


def _spawn(db, model: str, *, parent: str = "destination", project: str | None = PROJECT,
           account_id: str | None = None) -> str:
    """Create the child the way a real spawn does, so its binding is the launched answer."""
    from subagents.child_thread import create_child_thread

    create_child_thread(user_id="user", parent_thread_id=parent, thread_id="child",
                        model=model, project_id=project, account_id=account_id, db=db)
    return "child"


def _bound(db, provider: str) -> str | None:
    return chosen_account_id("child", provider, db=db)


def _turn_frozen(db, model: str) -> str | None:
    """The account the child's first Turn freezes — the owner ``start_idle_turn`` writes from."""
    from infra.vendor_turn_binding import turn_account_id

    return turn_account_id("child", model, db=db)


def _nothing_landed(db) -> None:
    """A refused creation leaves no child and no binding behind."""
    assert db.execute("SELECT COUNT(*) FROM threads WHERE id='child'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts WHERE thread_id='child'").fetchone()[0] == 0


def _workspace_parent(db) -> None:
    """A thread governed by no project, so only the workspace policy reaches its children."""
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
               "VALUES ('workspace-parent','user','codex/test','t','t')")
    db.commit()


@pytest.fixture
def claude(accounts):  # noqa: F811: the shared scope fixture is extended, not replaced
    """The same project, with a Claude Code harness, Default and Second account usable for one model."""
    from connections.models import upsert_connection
    from vendor_account_fixtures import signed_in_default

    db = accounts[0]
    db.execute("INSERT OR IGNORE INTO models(id,display_name,litellm_model,provider,provider_model_id,"
               "harness,created_at,updated_at) "
               "VALUES ('claude-code/sonnet','Sonnet','claude-code/sonnet','claude-code','sonnet',"
               "'claude-code','t','t')")
    db.commit()
    upsert_connection(type="cli", provider="claude-code", status="available",
                      config=DISCOVERED | {"binary": "/bin/test"})
    default = signed_in_default("claude-code", db=db)
    second = add_account("claude-code", "Claude Second", db=db)
    second = set_account_state(second["id"], "signed_in", expected_revision=0, db=db)
    return db, default, second


def _rollover_on_with_signed_out_default(db, provider: str, model: str, default, second) -> dict:
    """The reported shape: a permitted ready sibling, the signed-out default first in the saved order."""
    _usable(db, provider, default, model)
    _usable(db, provider, second, model)
    set_legacy_policy(PROJECT, provider, allowlist=[default["id"], second["id"]],
                      default_account_id=default["id"], switch_on_limit=True, db=db)
    _sign_out(db, default)
    return get_account(second["id"], db=db)


# --- the decisive pair: same-project actor duplication, both harness vendors ----------------


def test_codex_child_binds_the_account_the_project_catalog_projects(accounts):  # noqa: F811
    """A child spawned from a thread in its own project runs on the projected account."""
    db, default, second, _third = accounts
    _rollover_on_with_signed_out_default(db, "codex", "codex/test", default, second)

    projected, runnable = _projected(db, "codex", "codex/test")
    assert (projected["account_id"], projected["status"]) == (second["id"], "ready")
    assert runnable["status"] == "ready", runnable

    child = _spawn(db, "codex/test")
    assert _bound(db, "codex") == projected["account_id"]
    assert _turn_frozen(db, "codex/test") == projected["account_id"]


def test_claude_child_binds_the_account_the_project_catalog_projects(claude):
    """The same promise for the other harness vendor."""
    db, default, second = claude
    _rollover_on_with_signed_out_default(db, "claude-code", "claude-code/sonnet", default, second)

    projected, runnable = _projected(db, "claude-code", "claude-code/sonnet")
    assert (projected["account_id"], projected["status"]) == (second["id"], "ready")
    assert runnable["status"] == "ready", runnable

    child = _spawn(db, "claude-code/sonnet")
    assert _bound(db, "claude-code") == projected["account_id"]
    assert _turn_frozen(db, "claude-code/sonnet") == projected["account_id"]


# --- what the refusal must never become ----------------------------------------------------


def test_rollover_off_keeps_a_signed_out_default_refusing_every_surface(accounts):  # noqa: F811
    """With the switch off nothing moves, and projection and launch name the same refusal."""
    db, default, second, _third = accounts
    set_legacy_policy(PROJECT, "codex", allowlist=[default["id"], second["id"]],
                      default_account_id=default["id"], switch_on_limit=False, db=db)
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    _sign_out(db, default)

    projected, runnable = _projected(db, "codex", "codex/test")
    assert projected["account_id"] == default["id"]
    assert runnable == {"status": "unavailable", "status_reason": "account_signed_out"}
    with pytest.raises(AccountAdmissionError) as refused:
        _spawn(db, "codex/test")
    assert refused.value.reason == "account_signed_out"


def test_an_explicit_signed_out_account_never_rolls_over(accounts):  # noqa: F811
    """A caller's named account is its own choice, however the project is switched."""
    db, default, second, _third = accounts
    _rollover_on_with_signed_out_default(db, "codex", "codex/test", default, second)

    with pytest.raises(AccountAdmissionError) as refused:
        _spawn(db, "codex/test", account_id=default["id"])
    assert refused.value.reason == "account_signed_out"


def test_a_denied_scope_offers_no_other_account(accounts):  # noqa: F811
    """Denial stays denial: no permitted account is substituted for the scope's own answer."""
    db, default, second, _third = accounts
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    set_legacy_policy(PROJECT, "codex", allowlist=[], default_account_id=None, switch_on_limit=True, db=db)

    projected, runnable = _projected(db, "codex", "codex/test")
    assert projected["account_id"] is None
    assert runnable["status_reason"] == "account_project_denied"
    with pytest.raises(AccountAdmissionError) as refused:
        _spawn(db, "codex/test")
    assert refused.value.reason == "account_project_denied"


def test_a_cross_project_child_never_binds_an_account_the_actor_denies(accounts):  # noqa: F811
    """The child is admitted under destination AND actor; a fix must keep that intersection."""
    db, default, second, _third = accounts
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    set_legacy_policy(PROJECT, "codex", allowlist=[default["id"], second["id"]],
                      default_account_id=default["id"], switch_on_limit=True, db=db)
    set_legacy_policy(ACTOR_PROJECT, "codex", allowlist=[], default_account_id=None,
                      switch_on_limit=True, db=db)
    _sign_out(db, default)

    with pytest.raises(AccountAdmissionError) as refused:
        _spawn(db, "codex/test", parent="actor", project=PROJECT)
    assert refused.value.reason == "account_project_denied"
    _nothing_landed(db)


# --- the deliberate boundary: two policies still answer twice, on purpose -------------------


def test_two_projects_with_the_same_policy_still_do_not_move(accounts):  # noqa: F811
    """Distinct projects keep the plain candidate even when their policies are written identically."""
    db, default, second, _third = accounts
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    for project in (PROJECT, ACTOR_PROJECT):
        set_legacy_policy(project, "codex", allowlist=[default["id"], second["id"]],
                          default_account_id=default["id"], switch_on_limit=True, db=db)
    _sign_out(db, default)

    projected, _runnable = _projected(db, "codex", "codex/test")
    assert projected["account_id"] == second["id"]
    with pytest.raises(AccountAdmissionError) as refused:
        _spawn(db, "codex/test", parent="actor", project=PROJECT)
    assert refused.value.reason == "account_signed_out"
    _nothing_landed(db)


def test_a_workspace_actor_into_a_project_does_not_move(accounts):  # noqa: F811
    """An unprojected actor inherits the workspace policy yet still governs a second scope."""
    db, default, second, _third = accounts
    _workspace_parent(db)
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    set_legacy_policy(None, "codex", allowlist=[default["id"], second["id"]],
                      default_account_id=default["id"], switch_on_limit=True, db=db)
    _sign_out(db, default)

    with pytest.raises(AccountAdmissionError) as refused:
        _spawn(db, "codex/test", parent="workspace-parent", project=PROJECT)
    assert refused.value.reason == "account_signed_out"
    _nothing_landed(db)


def test_two_workspace_scopes_are_one_policy_and_move(accounts):  # noqa: F811
    """An unprojected child of an unprojected thread is governed by the workspace policy alone."""
    db, default, second, _third = accounts
    _workspace_parent(db)
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    set_legacy_policy(None, "codex", allowlist=[default["id"], second["id"]],
                      default_account_id=default["id"], switch_on_limit=True, db=db)
    _sign_out(db, default)

    _spawn(db, "codex/test", parent="workspace-parent", project=None)
    assert _bound(db, "codex") == second["id"]


# --- the detached reviewer consumer ---------------------------------------------------------


def _reviewer_target(provider: str, model: str) -> SimpleNamespace:
    """The fields ``resolve_scope_reviewer_account`` reads off the resolved review target."""
    return SimpleNamespace(harness=provider, catalog_id=model)


def test_reviewer_characterization_refuses_before_its_own_move(accounts, monkeypatch):  # noqa: F811
    """Characterizes today's reviewer refusal (passes on base); it is not the acceptance regression.

    It pins where the refusal happens: the reason gate at reviewer_account.py:76 fires before the
    reviewer's own ``rollover_destination`` call, so the hidden-selector base override below it is
    never reached and is not this surface's cause.
    """
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, default, second, _third = accounts
    _rollover_on_with_signed_out_default(db, "codex", "codex/test", default, second)

    def no_base(*_args, **_kwargs):
        raise AssertionError("the reviewer read its hidden-selector base before deciding the account")

    monkeypatch.setattr("llm.model_status.harness_base_row", no_base)
    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_reviewer_target("codex", "codex/test"), PROJECT, db=db)
    assert refused.value.reason == "account_signed_out"


def test_reviewer_moves_off_a_limited_default_with_the_switch_on(accounts):  # noqa: F811
    """The same reviewer does take its project's move when no lifecycle reason gates it first."""
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, default, second, _third = accounts
    _usable(db, "codex", default, "codex/test")
    _usable(db, "codex", second, "codex/test")
    set_legacy_policy(PROJECT, "codex", allowlist=[default["id"], second["id"]],
                      default_account_id=default["id"], switch_on_limit=True, db=db)
    _limited(db, "codex", default, "codex/test")

    assert resolve_scope_reviewer_account(_reviewer_target("codex", "codex/test"), PROJECT, db=db) == second["id"]
