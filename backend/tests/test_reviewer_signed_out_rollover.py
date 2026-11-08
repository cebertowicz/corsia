"""A signed-out first account still takes the reviewer project's existing opt-in move.

The detached reviewer has no thread, so its scope answer is the account its project would use
before any model-specific move. That answer's lifecycle reason used to end the decision before the
project's own ``rollover_destination`` was asked, so a project that had opted into switching could
not review while its Default was signed out even with a permitted, signed-in, usage-allowed
sibling. These cases drive the real owners only: ``scope_account``, ``rollover_destination``,
``permitted_accounts``, ``account_model_status``, and the detached admission in ``reviewer_launch``
through ``admit_operation``, ``operation_directory`` and ``require_operation``. Nothing on that path
is mocked; only a vendor's own control read (``schedule_account_check``) is intercepted, so no real
vendor is ever asked. Synthetic accounts only: no vendor call, login, secret, real account or
persistent binding is touched.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import accounts  # noqa: F401  # the shared synthetic account scope

MODEL = {"codex": "codex/test", "claude-code": "claude-code/sonnet"}
PROJECT = "destination-project"


def _target(provider: str, model: str | None = None) -> SimpleNamespace:
    """The fields the detached resolver reads off the already-resolved review target."""
    return SimpleNamespace(harness=provider, catalog_id=model or MODEL[provider])


def _usable(db, provider: str, account, *models: str) -> None:
    """One signed-in account with a login, ready discovery for each model, and a recorded 'allowed'."""
    from connections import harness_usage
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt(provider, account_id=account["id"], db=db)
    assert receipt is not None, account["id"]
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models={model: {"available": True, "resolved_identity": "synthetic"}
                                  for model in models}, db=db)
    db.commit()  # usage records its own write scope; an ambient transaction here would nest
    harness_usage.record_allowed(provider=provider, receipt=receipt, source="discovery", db=db)
    db.commit()


def _limited(db, provider: str, account) -> None:
    from connections import harness_usage
    from connections.account_receipts import capture_receipt

    harness_usage.record_limited(provider=provider,
                                 receipt=capture_receipt(provider, account_id=account["id"], db=db),
                                 reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                                 source="rate_limit_update", db=db)
    db.commit()


def _signed_out(db, account) -> None:
    from connections.vendor_accounts import get_account, set_account_state

    current = get_account(account["id"], db=db)
    set_account_state(account["id"], "signed_out", expected_revision=current["revision"], db=db)
    db.commit()


def _policy(db, provider: str, *, allowlist, default_id, switch) -> None:
    from vendor_account_fixtures import set_legacy_policy

    set_legacy_policy(PROJECT, provider, allowlist=allowlist, default_account_id=default_id,
                      switch_on_limit=switch, db=db)
    db.commit()


def _hidden_selector_row(db, provider: str, model: str) -> None:
    """Register the provider-owned review selector the user catalog hides: internal, no observed window."""
    db.execute("INSERT OR IGNORE INTO models(id,display_name,litellm_model,provider,provider_model_id,"
               "harness,enabled,available,context_window,metadata_json,created_at,updated_at) "
               "VALUES (?,'Review',?,?,?,?,'0','1',NULL,?,?,?)",
               (model, model, provider, model.rsplit("/", 1)[-1], provider,
                '{"internal_reviewer": true, "resolved_identity": "review-selector"}', "t", "t"))
    db.commit()


@pytest.fixture
def codex_scope(accounts):  # the shared codex scope is extended, not replaced
    """The reported shape for Codex: a signed-in permitted sibling beside the project Default."""
    db, default, second, _third = accounts
    return db, "codex", default, second


@pytest.fixture
def claude_scope(accounts):  # the shared codex scope is extended, not replaced
    """The same shape for Claude Code, whose harness row and connection this test owns."""
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state
    from vendor_account_fixtures import DISCOVERED, signed_in_default

    db = accounts[0]
    db.execute("INSERT OR IGNORE INTO models(id,display_name,litellm_model,provider,provider_model_id,"
               "harness,created_at,updated_at) VALUES (?,'Review',?,?,?,?,'t','t')",
               (MODEL["claude-code"], MODEL["claude-code"], "claude-code", "sonnet", "claude-code"))
    db.commit()
    upsert_connection(type="cli", provider="claude-code", status="available",
                      config=DISCOVERED | {"binary": "/bin/synthetic"})
    default = signed_in_default("claude-code", db=db, models=(MODEL["claude-code"],))
    second = add_account("claude-code", "Claude Second", db=db)
    second = set_account_state(second["id"], "signed_in", expected_revision=0, db=db)
    return db, "claude-code", default, second


@pytest.fixture(params=["codex_scope", "claude_scope"])
def scope(request):
    """Each harness vendor's detached reviewer consumer, from accounts of the same shape."""
    return request.getfixturevalue(request.param)


def _switch_on_signed_out(db, provider: str, default, second, *, destination="allowed", selector=None):
    """Opt-in rollover with the signed-out Default first in the saved order and a ready sibling."""
    models = (MODEL[provider],) if selector is None else (MODEL[provider], selector)
    _usable(db, provider, default, *models)
    _usable(db, provider, second, *models)
    _policy(db, provider, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)
    if destination == "limited":
        _limited(db, provider, second)
    elif destination == "unknown":
        # Signed in and ready with no usage reading at all: unknown is visible but never a destination.
        from connections import harness_usage

        harness_usage.clear_account_usage(provider, second["id"], db=db)
        db.commit()
    _signed_out(db, default)


@contextlib.contextmanager
def _reviewer_operation(scope, monkeypatch, project: str = PROJECT):
    """The reviewer's own detached launch, with its admitted homes and environment while they live."""
    from reviewer import reviewer_account

    provider = scope[1]
    monkeypatch.setattr(reviewer_account, "_reviewer_project_id", lambda _db: project)
    with reviewer_account.reviewer_launch(_target(provider)) as env:
        yield env


def _refuses_build_no_environment(scope, monkeypatch, project: str = PROJECT):
    """A refused decision must not admit an operation or build any launch environment."""
    from reviewer.failures import CCUnavailableError

    with contextlib.suppress(CCUnavailableError):
        with _reviewer_operation(scope, monkeypatch, project):
            pytest.fail("a refused reviewer decision built a launch environment")


def test_a_signed_out_source_still_takes_the_projects_move(scope, monkeypatch):
    """The permitted, signed-in, usage-allowed destination is chosen and admitted on its own homes."""
    from connections.vendor_accounts import get_account
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _switch_on_signed_out(db, provider, default, second)

    assert resolve_scope_reviewer_account(_target(provider), PROJECT, db=db) == second["id"]

    source_home = Path(default["source_home"]).resolve()
    destination_home = Path(second["source_home"]).resolve()
    with _reviewer_operation(scope, monkeypatch) as env:
        if provider == "codex":
            home = Path(env["CODEX_HOME"]).resolve()
            # The launch home is the managed detached home, never either account's source home, and
            # the login it projects is the destination's, not the signed-out source's.
            assert home != destination_home and home != source_home
            assert Path(env["CODEX_SQLITE_HOME"]).resolve() == home
            projected = (home / "auth.json").resolve()
            assert projected.is_relative_to(destination_home)
            assert not projected.is_relative_to(source_home)
        else:
            assert Path(env["CLAUDE_CONFIG_DIR"]).resolve() == destination_home
        # Frozen at the destination while the operation is live: no thread binding was written and
        # neither account's lifecycle changed.
        assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts").fetchone()[0] == 0
        assert get_account(second["id"], db=db)["state"] == "signed_in"
        assert get_account(default["id"], db=db)["state"] == "signed_out"


def test_rollover_off_keeps_the_signed_out_cause_and_builds_no_environment(scope, monkeypatch):
    """With the switch off nothing moves: the source's own cause stays the visible refusal."""
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _usable(db, provider, default, MODEL[provider])
    _usable(db, provider, second, MODEL[provider])
    _policy(db, provider, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=False)
    _signed_out(db, default)

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db)
    assert refused.value.reason == "account_signed_out"
    _refuses_build_no_environment(scope, monkeypatch)


def test_an_unusable_destination_keeps_the_original_cause(scope, monkeypatch):
    """A destination that cannot run the model is no destination: the source cause stays visible."""
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _switch_on_signed_out(db, provider, default, second, destination="limited")

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db)
    assert refused.value.reason == "account_signed_out"
    _refuses_build_no_environment(scope, monkeypatch)


def test_an_unknown_usage_destination_is_never_dispatched(scope, monkeypatch):
    """A move never rests on unread usage: that candidate only gets its existing usage read."""
    import connections.harness_usage_check as usage_check
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _switch_on_signed_out(db, provider, default, second, destination="unknown")
    scheduled = []
    monkeypatch.setattr(usage_check, "schedule_account_check",
                        lambda _provider, identity, **_kwargs: scheduled.append(identity))

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db)

    assert refused.value.reason == "account_signed_out"
    assert scheduled == [second["id"]]
    _refuses_build_no_environment(scope, monkeypatch)


@pytest.mark.parametrize("refusal", ["project_denied", "project_missing"])
def test_a_denied_or_unconfigured_scope_never_finds_a_destination(scope, monkeypatch, refusal):
    """Denial stays denial and a missing scope policy stays a refusal: no ready account is borrowed."""
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _switch_on_signed_out(db, provider, default, second)
    project = PROJECT
    if refusal == "project_denied":
        _policy(db, provider, allowlist=[], default_id=None, switch=True)
    else:
        project = "no-such-project"

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), project, db=db)

    assert refused.value.reason == ("account_project_denied" if refusal == "project_denied"
                                    else "project_policy_missing")
    _refuses_build_no_environment(scope, monkeypatch, project)


def test_no_signed_in_account_at_all_still_refuses(scope, monkeypatch):
    """With no source to move from, the refusal is the scope's own, never the ambient login."""
    from connections.vendor_accounts import list_accounts
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _usable(db, provider, default, MODEL[provider])
    _usable(db, provider, second, MODEL[provider])
    everyone = [account["id"] for account in list_accounts(provider=provider, db=db)]
    _policy(db, provider, allowlist=everyone, default_id=default["id"], switch=True)
    # Every account of this provider goes, so there is no source left to move from.
    db.execute(f"UPDATE vendor_accounts SET state='removed' WHERE id IN ({','.join('?' * len(everyone))})",
               tuple(everyone))
    db.commit()

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db)
    assert refused.value.reason in {"account_removed", "account_not_configured"}
    _refuses_build_no_environment(scope, monkeypatch)


def test_a_hidden_reviewer_selector_still_decides_the_model(scope, monkeypatch):
    """What the user catalog cannot show for the review selector is not a reason to refuse an account."""
    from llm.model_status import harness_base_row
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    selector = f"{MODEL[provider]}[1m]"
    _hidden_selector_row(db, provider, selector)
    _switch_on_signed_out(db, provider, default, second, selector=selector)
    # The row the user catalog hides: no observed window, so it is never shown as ready.
    assert harness_base_row(selector, db=db)["status"] != "ready"

    assert resolve_scope_reviewer_account(_target(provider, selector), PROJECT, db=db) == second["id"]


def test_a_hidden_reviewer_selector_still_asks_the_account(scope, monkeypatch):
    """Hiding the selector never waives the account's own readiness for that model."""
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    selector = f"{MODEL[provider]}[1m]"
    _hidden_selector_row(db, provider, selector)
    # Both accounts are ready for the project model but have never listed the review selector.
    _usable(db, provider, default, MODEL[provider])
    _usable(db, provider, second, MODEL[provider])
    _policy(db, provider, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider, selector), PROJECT, db=db)
    assert refused.value.reason == "account_model_not_discovered"


def test_a_deferred_reason_grants_no_launch(scope, monkeypatch):
    """A reason held back for rollover still refuses when no permitted destination exists."""
    import connections.account_policy as account_policy
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _switch_on_signed_out(db, provider, default, second)
    # A selector that names the now-denied Default while the project permits no account at all.
    monkeypatch.setattr(account_policy, "scope_account",
                        lambda *_a, **_k: (default["id"], "account_signed_out"))
    _policy(db, provider, allowlist=[], default_id=None, switch=True)

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db)
    assert refused.value.reason == "account_signed_out"

    # Containment is unchanged: a named account outside the permitted set is still refused at the seam.
    monkeypatch.setattr(account_policy, "scope_account", lambda *_a, **_k: (default["id"], None))
    _policy(db, provider, allowlist=[second["id"]], default_id=second["id"], switch=False)

    with pytest.raises(CCUnavailableError) as outside:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db)
    assert outside.value.reason == "account_project_denied"
    _refuses_build_no_environment(scope, monkeypatch)


def test_a_pending_choice_is_not_moved_by_the_reviewer(scope):
    """A choice made during a running turn stays the thread's answer, reviewer included.

    Not a reviewer journey of its own: the lease-caller tests cover the thread path from the
    requester's side. It is here because the waited lifecycle reason is gated on the same branch, and
    a move past a turn account would override that choice with the sibling this project permits.
    """
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db, provider, default, second = scope
    _usable(db, provider, default, MODEL[provider])
    _usable(db, provider, second, MODEL[provider])
    _policy(db, provider, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)
    _limited(db, provider, second)
    db.execute("INSERT INTO threads(id,user_id,title,model,project_id,created_at,updated_at) "
               "VALUES ('requester','user','requester',?,?,'t','t')", (MODEL[provider], PROJECT))
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id,pending_account_id) "
               "VALUES ('requester',?,?,?)", (provider, default["id"], second["id"]))
    db.commit()

    with pytest.raises(CCUnavailableError) as refused:
        resolve_scope_reviewer_account(_target(provider), PROJECT, db=db, thread_id="requester")

    assert refused.value.reason == "usage_limited"
