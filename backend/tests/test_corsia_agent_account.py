"""The first-run bind step: a model-less workspace conductor bound to the account a person chose.

Pins: only a Ready account is bound, as a first binding or a move; a sign-in's bind is
stale once a newer refresh or login replaced it; each refusal writes nothing; the
card's mark reads only the stored binding and survives the first pick (r15 marks
(a)-(e)); a sign-in started with ``bind_thread_id`` reports its outcome; and J7 runs
end to end: the person's own account, signed in on the card, runs the first turn."""
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from connections import account_controls, harness_models, harness_usage
from connections.account_discovery_types import AccountRefreshResult
from connections.account_evidence import mark_discovery
from connections.account_policy import chosen_account_id, select_account, turn_account
from connections.corsia_agent_account import (
    FirstRunBind,
    NotCorsiaAgent,
    RefreshObservation,
    bind_corsia_agent_account,
    source_accounts,
)
from connections.provider_policy import effective_policy, set_policy
from connections.vendor_accounts import add_account, get_account, set_account_state
from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
from infra.database import write_transaction
from infra.time import utc_now_iso
from vendor_account_fixtures import accounts, receipt_of, set_legacy_policy  # noqa: F401

CONDUCTOR, MODEL = "conductor", "codex/test"
OUTCOMES = ("ready", "limited", "no_usable_model", "unchecked")


class ExitedLogin:
    returncode = 0

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()

    async def wait(self):
        return 0


@pytest.fixture
def conductor(accounts):
    """A model-less workspace conductor and two signed-in added Codex accounts, Work and Personal."""
    db, _legacy, work, personal = accounts
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES (?,'user',NULL,'t','t')",
               (CONDUCTOR,))
    db.commit()
    assert set_corsia_agent(CONDUCTOR, db=db)
    db.commit()
    return db, work, personal


def _discover(db, account, models=None):
    receipt = receipt_of("codex", account["id"], db=db)
    assert mark_discovery(account["id"], receipt=receipt, status="ready",
                          models=models or {MODEL: {"available": True}}, db=db)
    db.commit()


def _make(outcome, db, account):
    """Leave ``account`` in one of the four after-refresh states."""
    if outcome == "unchecked":
        return
    _discover(db, account, None if outcome != "no_usable_model" else {MODEL: {"available": False}})
    if outcome == "limited":
        harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", account["id"], db=db),
                                     reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                                     source="rate_limit_update", db=db)


def _observed(db, account):
    """What this sign-in's own refresh left, read the moment it returned."""
    return RefreshObservation(receipt_of("codex", account["id"], db=db), utc_now_iso())


def _bindings(db):
    return dict(db.execute("SELECT provider,account_id FROM thread_vendor_accounts WHERE thread_id=?", (CONDUCTOR,)))


def _switches(db):
    return [json.loads(row[0]) for row in db.execute(
        "SELECT metadata FROM messages WHERE thread_id=? AND json_extract(metadata,'$.type')='account_switch'",
        (CONDUCTOR,))]


def _marked(provider="codex"):
    return [line["account_id"] for line in source_accounts(provider) if line["corsia_agent_binding"]]


@pytest.mark.parametrize("path", ["first_binding", "move"])
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_only_a_ready_refresh_writes_a_first_binding_or_a_move(conductor, path, outcome):
    db, work, personal = conductor
    if path == "move":
        select_account(CONDUCTOR, "codex", account_id=work["id"], db=db)
    before, policy = _bindings(db), effective_policy(None, "codex", db=db)
    _make(outcome, db, personal)

    result = bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user", observed=_observed(db, personal))

    assert result == {"bound": outcome == "ready", "reason": None if outcome == "ready" else outcome}
    assert _bindings(db) == ({"codex": personal["id"]} if outcome == "ready" else before)
    assert [switch["reason"] for switch in _switches(db)] == (
        ["account_pinned"] if outcome == "ready" and path == "move" else [])
    # The workspace default is never the bind step's to change.
    assert effective_policy(None, "codex", db=db) == policy


@pytest.mark.parametrize("newer", ["refresh", "refresh_failure", "login"])
def test_a_bind_on_a_replaced_refresh_is_stale_and_writes_nothing(conductor, newer):
    db, _work, personal = conductor
    _discover(db, personal)
    observed = RefreshObservation(receipt_of("codex", personal["id"], db=db),
                                  (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(tzinfo=None).isoformat())
    if newer == "refresh":
        _discover(db, personal)
    elif newer == "refresh_failure":
        # A run that cannot finish rides beside the last good read as its refresh failure.
        assert mark_discovery(personal["id"], receipt=observed.receipt, status="failed", cause="synthetic", db=db)
        db.commit()
    else:
        out = set_account_state(personal["id"], "signed_out", expected_revision=get_account(personal["id"])["revision"])
        set_account_state(personal["id"], "signed_in", expected_revision=out["revision"])

    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user", observed=observed) == {
        "bound": False, "reason": "stale"}
    assert _bindings(db) == {}


def test_a_person_choosing_an_account_they_already_have_reads_its_current_models(conductor):
    db, _work, personal = conductor
    _discover(db, personal)
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user") == {"bound": True, "reason": None}
    assert _bindings(db) == {"codex": personal["id"]}


@pytest.mark.parametrize("why", ["model", "pending_model", "archived", "queued_write"])
def test_a_conductor_that_has_started_refuses_the_bind(conductor, why):
    db, _work, personal = conductor
    _discover(db, personal)
    if why == "queued_write":
        from infra.input_admission import accept_thread_input

        accept_thread_input(CONDUCTOR, role="user", content="hi", intent="apply_now", message_id="m", db=db)
        db.execute("UPDATE messages SET write_started_at='started' WHERE id='m'")
    else:
        column, value = {"model": ("model", MODEL), "pending_model": ("pending_model", MODEL),
                         "archived": ("lifecycle_state", "archived")}[why]
        db.execute(f"UPDATE threads SET {column}=? WHERE id=?", (value, CONDUCTOR))
    db.commit()

    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user") == {
        "bound": False, "reason": "corsia_agent_started"}
    assert _bindings(db) == {}


@pytest.mark.parametrize("how", ["unknown", "removed"])
def test_a_missing_account_refuses_the_bind(conductor, how):
    db, _work, personal = conductor
    _discover(db, personal)
    account_id = "no-such-account"
    if how == "removed":
        set_account_state(personal["id"], "removed", expected_revision=get_account(personal["id"])["revision"])
        account_id = personal["id"]

    assert bind_corsia_agent_account(CONDUCTOR, account_id, user_id="user") == {
        "bound": False, "reason": "account_missing"}
    assert _bindings(db) == {}


def test_an_account_turned_off_for_the_workspace_is_not_permitted(conductor):
    db, work, personal = conductor
    _discover(db, personal)
    set_legacy_policy(None, "codex", allowlist=[work["id"]], db=db)

    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user", observed=_observed(db, personal)) == {
        "bound": False, "reason": "not_permitted"}
    assert _bindings(db) == {}


@pytest.mark.parametrize("thread", ["other", "not_owned"])
def test_only_this_users_workspace_conductor_can_be_bound(conductor, thread):
    db, _work, personal = conductor
    _discover(db, personal)
    if thread == "other":
        target, user = "destination", "user"
    else:
        target, user = CONDUCTOR, "someone-else"
    with pytest.raises(NotCorsiaAgent):
        bind_corsia_agent_account(target, personal["id"], user_id=user)
    assert _bindings(db) == {}


# r15 section 6, the card's mark.

def test_mark_a_with_no_binding_no_account_is_marked(conductor):
    db, work, personal = conductor
    _discover(db, work)
    _discover(db, personal)
    lines = source_accounts("codex")
    assert {line["account_id"] for line in lines} >= {work["id"], personal["id"]}
    assert _marked() == []


def _pick(model=MODEL, account_id=None):
    from thread.model_row import select_first_model

    assert select_first_model(CONDUCTOR, model, user_id="user", now="t", account_id=account_id)


def test_mark_b_with_rollover_off_the_marked_account_is_bound_after_the_pick(conductor):
    db, work, personal = conductor
    _discover(db, work)
    _discover(db, personal)
    set_legacy_policy(None, "codex", switch_on_limit=False, db=db)
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")["bound"]
    assert _marked() == [personal["id"]]

    _pick()

    assert chosen_account_id(CONDUCTOR, "codex", db=db) == personal["id"]


@pytest.mark.parametrize("limited", [False, True])
def test_mark_c_with_rollover_on_the_pick_keeps_a_usable_binding_and_moves_a_limited_one(conductor, limited):
    db, work, personal = conductor
    _discover(db, work)
    _discover(db, personal)
    set_legacy_policy(None, "codex", allowlist=[work["id"], personal["id"]], switch_on_limit=True,
                      default_account_id=work["id"], db=db)
    # A rollover destination's vendor has said it is allowed: a move never rests on unknown usage.
    harness_usage.record_allowed(provider="codex", receipt=receipt_of("codex", work["id"], db=db),
                                 source="discovery", db=db)
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")["bound"]
    assert _marked() == [personal["id"]]
    if limited:
        harness_usage.record_limited(provider="codex", receipt=receipt_of("codex", personal["id"], db=db),
                                     reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                                     source="rate_limit_update", db=db)

    _pick()

    assert chosen_account_id(CONDUCTOR, "codex", db=db) == (work["id"] if limited else personal["id"])
    # The bind was a first binding, so the only move is the pick's rollover.
    assert [switch["reason"] for switch in _switches(db)] == (["account_limit"] if limited else [])


def _bound_personal(db, work, personal, *, rollover):
    """Both accounts usable, the conductor bound to Personal, rollover as asked."""
    _discover(db, work)
    _discover(db, personal)
    set_legacy_policy(None, "codex", allowlist=[work["id"], personal["id"]], switch_on_limit=rollover,
                      default_account_id=work["id"], db=db)
    harness_usage.record_allowed(provider="codex", receipt=receipt_of("codex", work["id"], db=db),
                                 source="discovery", db=db)
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")["bound"]


def _lose(db, personal, how):
    if how == "removed":
        set_account_state(personal["id"], "removed", expected_revision=get_account(personal["id"])["revision"])
    elif how == "signed_out":
        set_account_state(personal["id"], "signed_out", expected_revision=get_account(personal["id"])["revision"])
    else:
        set_policy(None, "codex", rollover=True, order=[personal["id"]], disabled=[personal["id"]],
                   expected_revision=effective_policy(None, "codex", db=db)["revision"], db=db)
    db.commit()


_LOST = {"signed_out": "account_signed_out", "removed": "account_removed", "turned_off": "account_not_permitted"}


@pytest.mark.parametrize("how", list(_LOST))
def test_mark_f_with_rollover_on_the_pick_moves_a_lost_binding_and_the_receipt_names_why(conductor, how):
    db, work, personal = conductor
    _bound_personal(db, work, personal, rollover=True)
    _lose(db, personal, how)

    _pick()

    assert chosen_account_id(CONDUCTOR, "codex", db=db) == work["id"]
    # The reason is the real one, never an account limit.
    assert [switch["reason"] for switch in _switches(db)] == [_LOST[how]]
    # The chat row's summary is reason-independent text, never blank or an enum name.
    [content] = [row[0] for row in db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND json_extract(metadata,'$.type')='account_switch'",
        (CONDUCTOR,))]
    assert content.startswith("Moved to ") and _LOST[how] not in content


@pytest.mark.parametrize("how", ["signed_out", "removed"])
def test_mark_f_with_rollover_off_the_pick_is_refused_and_no_model_is_saved(conductor, how):
    from connections.account_policy import AccountAdmissionError

    db, work, personal = conductor
    _bound_personal(db, work, personal, rollover=False)
    _lose(db, personal, how)

    with pytest.raises(AccountAdmissionError) as refused:
        _pick()

    assert refused.value.reason == {"signed_out": "account_signed_out", "removed": "account_removed"}[how]
    assert db.execute("SELECT model FROM threads WHERE id=?", (CONDUCTOR,)).fetchone()[0] is None
    assert _bindings(db) == {"codex": personal["id"]} and _switches(db) == []


@pytest.mark.parametrize("how", ["signed_out", "removed", "turned_off"])
def test_mark_f_at_the_claim_only_a_turned_off_binding_moves_and_a_signed_out_or_removed_one_is_held(conductor, how):
    from connections.account_rollover import commit_rollover, plan_rollover
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import _claimable_prefix

    db, work, personal = conductor
    _bound_personal(db, work, personal, rollover=True)
    _pick()
    accept_thread_input(CONDUCTOR, role="user", content="hello", intent="apply_now", message_id="queued", db=db)
    _lose(db, personal, how)
    # Only a project not permitting the account is a movable hold: the claim's prefix is offered to a move.
    # The prefix is read inside the claim's writer transaction, as start_idle_turn reads it.
    with write_transaction(db, scope="test_claim_prefix"):
        head = _claimable_prefix(db, CONDUCTOR, movable=True)

    if how == "turned_off":
        assert head
        with write_transaction(db, scope="test_claim_rollover"):
            assert commit_rollover(plan_rollover(CONDUCTOR, eligible_rows=head[2], db=db), db=db) is not None
        assert chosen_account_id(CONDUCTOR, "codex", db=db) == work["id"]
        assert [switch["reason"] for switch in _switches(db)] == ["account_not_permitted"]
    else:
        assert head is None
        assert _bindings(db) == {"codex": personal["id"]} and _switches(db) == []


@pytest.mark.parametrize("how", ["removed", "turned_off"])
def test_mark_f_before_the_pick_use_this_account_rebinds_a_lost_binding_to_the_one_survivor(conductor, how):
    db, work, personal = conductor
    _bound_personal(db, work, personal, rollover=False)
    _lose(db, personal, how)
    assert _marked() == []

    assert bind_corsia_agent_account(CONDUCTOR, work["id"], user_id="user") == {"bound": True, "reason": None}
    assert _marked() == [work["id"]]
    _pick()

    assert chosen_account_id(CONDUCTOR, "codex", db=db) == work["id"]
    assert [switch["reason"] for switch in _switches(db)] == ["account_pinned"]


@pytest.mark.parametrize("how", ["removed", "turned_off"])
def test_mark_f_after_the_pick_choosing_the_survivor_rebinds_and_the_held_input_runs(conductor, how):
    from connections.account_selection import rebind_idle_account
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import _claimable_prefix

    db, work, personal = conductor
    _bound_personal(db, work, personal, rollover=False)
    _pick()
    accept_thread_input(CONDUCTOR, role="user", content="hello", intent="apply_now", message_id="queued", db=db)
    _lose(db, personal, how)
    with write_transaction(db, scope="test_claim_prefix"):
        assert _claimable_prefix(db, CONDUCTOR) is None  # held on the lost account

    rebind_idle_account(CONDUCTOR, work["id"], user_id="user", db=db)
    db.commit()

    assert chosen_account_id(CONDUCTOR, "codex", db=db) == work["id"]
    with write_transaction(db, scope="test_claim_prefix"):
        assert _claimable_prefix(db, CONDUCTOR) is not None


def test_mark_d_a_turned_off_account_is_refused_but_one_left_out_of_a_saved_order_is_bound(conductor):
    db, work, personal = conductor
    _discover(db, personal)
    revision = effective_policy(None, "codex", db=db)["revision"]
    set_policy(None, "codex", rollover=True, order=[work["id"]], disabled=[personal["id"]],
               expected_revision=revision, db=db)
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user", observed=_observed(db, personal)) == {
        "bound": False, "reason": "not_permitted"}
    assert _bindings(db) == {}

    set_policy(None, "codex", rollover=True, order=[work["id"]], disabled=[],
               expected_revision=revision + 1, db=db)
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user", observed=_observed(db, personal)) == {
        "bound": True, "reason": None}
    assert _bindings(db) == {"codex": personal["id"]}


def test_mark_e_an_explicit_choice_binds_that_account_with_or_without_a_model(conductor):
    from connections.account_selection import rebind_idle_account

    db, work, personal = conductor
    _discover(db, work)
    _discover(db, personal)
    # The model-less conductor: a card line's "Use this account" and the picker's explicit
    # account row each go through the bind step, then the pick runs on the account it names.
    assert bind_corsia_agent_account(CONDUCTOR, work["id"], user_id="user")["bound"]
    assert _marked() == [work["id"]]
    assert bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")["bound"]
    assert _marked() == [personal["id"]]
    _pick(account_id=personal["id"])
    assert chosen_account_id(CONDUCTOR, "codex", db=db) == personal["id"]
    assert [switch["reason"] for switch in _switches(db)] == ["account_pinned"]

    # A thread with a model: today's route binds the chosen account.
    select_account("destination", "codex", account_id=work["id"], db=db)
    rebind_idle_account("destination", personal["id"], user_id="user", db=db)
    assert chosen_account_id("destination", "codex", db=db) == personal["id"]


# The routes.

@pytest.fixture
def api(conductor):
    from api.vendor_accounts import router
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "user"}
    with TestClient(app) as client:
        yield client


def test_the_bind_route_returns_bound_and_reason_and_404s_any_other_thread(conductor, api):
    db, work, personal = conductor
    _discover(db, personal)

    assert api.post(f"/api/threads/{CONDUCTOR}/corsia-agent-account",
                    json={"account_id": work["id"]}).json() == {"bound": False, "reason": "unchecked"}
    assert api.post(f"/api/threads/{CONDUCTOR}/corsia-agent-account",
                    json={"account_id": personal["id"]}).json() == {"bound": True, "reason": None}
    assert api.post("/api/threads/destination/corsia-agent-account",
                    json={"account_id": personal["id"]}).status_code == 404
    assert _bindings(db) == {"codex": personal["id"]}


def test_a_sign_in_naming_a_thread_that_is_not_the_conductor_is_refused_before_it_starts(conductor, api):
    _db, _work, personal = conductor
    out = set_account_state(personal["id"], "signed_out", expected_revision=get_account(personal["id"])["revision"])
    assert out["state"] == "signed_out"
    response = api.post(f"/api/vendor-accounts/{personal['id']}/signin", json={"bind_thread_id": "destination"})
    assert response.status_code == 404
    assert personal["id"] not in account_controls._SIGNINS


# The sources payload.

def test_source_accounts_are_the_pickers_rows_with_the_stored_binding_marked(conductor):
    db, work, personal = conductor
    _discover(db, personal)
    removed = set_account_state(add_account("codex", "Gone")["id"], "removed", expected_revision=0)
    set_legacy_policy(None, "codex", allowlist=[work["id"], personal["id"]], db=db)
    bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")

    lines = {line["account_id"]: line for line in source_accounts("codex")}

    assert removed["id"] not in lines
    assert set(lines) == {work["id"], personal["id"]}
    assert lines[personal["id"]] == {"account_id": personal["id"], "label": lines[personal["id"]]["label"],
                                     "label_verified": False, "identified": True, "state": "ready",
                                     "resets_at": None, "corsia_agent_binding": True}
    assert (lines[work["id"]]["state"], lines[work["id"]]["corsia_agent_binding"]) == ("unchecked", False)


def test_a_limited_source_account_names_when_it_is_back(conductor):
    db, _work, personal = conductor
    _make("limited", db, personal)
    line = next(line for line in source_accounts("codex") if line["account_id"] == personal["id"])
    assert (line["state"], line["resets_at"]) == ("limited", "2099-01-01T00:00:00+00:00")


def test_needs_auth_source_projects_the_default_as_signed_out_without_mutating_its_lifecycle(conductor, monkeypatch):
    from connections.source_state import apply_check
    from connections.vendor_accounts import default_account

    monkeypatch.setattr("connections.harness_legacy_fallback.sync_legacy_fallback", lambda *_args, **_kwargs: None)
    db, _work, _personal = conductor
    default = default_account("claude-code", db=db)
    apply_check("claude-code", "needs_auth", "signed out", db=db)

    line = next(line for line in source_accounts("claude-code") if line["account_id"] == default["id"])

    assert default_account("claude-code", db=db)["state"] == "legacy"
    assert line["state"] == "signed_out"
    # The bind path reads the same state the card shows.
    assert bind_corsia_agent_account(CONDUCTOR, default["id"], user_id="user") == {
        "bound": False, "reason": "not_ready"}


def test_source_accounts_read_the_vendors_models_once_whatever_the_account_count(conductor):
    from infra.database import get_db

    db, work, personal = conductor
    _discover(db, personal)

    def model_reads() -> int:
        statements: list[str] = []
        traced = get_db()
        traced.set_trace_callback(statements.append)
        try:
            source_accounts("codex")
        finally:
            traced.set_trace_callback(None)
        return sum("FROM models" in statement for statement in statements)

    both = model_reads()
    # A signed-out account reads no models of its own, so one signed-in account is left reading.
    set_account_state(work["id"], "signed_out", expected_revision=get_account(work["id"])["revision"])
    assert both > 0 and model_reads() == both


def test_a_vendor_listing_no_model_has_no_account_lines(conductor):
    db, _work, personal = conductor
    _discover(db, personal)
    db.execute("UPDATE models SET enabled=0 WHERE harness='codex'")
    db.commit()
    assert source_accounts("codex") == []


def test_the_sources_route_carries_each_vendors_account_lines(conductor, monkeypatch):
    from api import provider_sources

    db, _work, personal = conductor
    _discover(db, personal)
    source = provider_sources._harness_source("codex")
    assert personal["id"] in {line["account_id"] for line in source["accounts"]}


def test_the_sources_payload_names_no_lost_binding_while_the_bound_account_is_usable(conductor):
    from api import provider_sources

    db, _work, personal = conductor
    _discover(db, personal)
    assert provider_sources._harness_source("codex")["lost_binding"] is None
    bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")
    assert provider_sources._harness_source("codex")["lost_binding"] is None


def test_the_sources_payload_names_a_removed_bound_account_as_the_lost_binding(conductor):
    from api import provider_sources

    db, _work, personal = conductor
    _discover(db, personal)
    bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")
    set_account_state(personal["id"], "removed", expected_revision=get_account(personal["id"])["revision"])

    source = provider_sources._harness_source("codex")

    assert source["lost_binding"] == {"label": source["lost_binding"]["label"], "reason": "removed"}
    assert source["lost_binding"]["label"] and personal["id"] not in {line["account_id"] for line in source["accounts"]}
    assert not any(line["corsia_agent_binding"] for line in source["accounts"])


def test_the_sources_payload_names_a_turned_off_bound_account_as_the_lost_binding(conductor):
    from api import provider_sources

    db, work, personal = conductor
    _discover(db, personal)
    bind_corsia_agent_account(CONDUCTOR, personal["id"], user_id="user")
    set_policy(None, "codex", rollover=False, order=[work["id"]], disabled=[personal["id"]],
               expected_revision=effective_policy(None, "codex", db=db)["revision"], db=db)
    db.commit()

    source = provider_sources._harness_source("codex")

    assert source["lost_binding"] == {"label": source["lost_binding"]["label"], "reason": "turned_off"}
    assert source["lost_binding"]["label"] and personal["id"] not in {line["account_id"] for line in source["accounts"]}


# A first-run sign-in reports its outcome once its own model refresh and bind finished.

async def _signin(monkeypatch, db, account, refresh, *, bind=CONDUCTOR):
    """Drive one finished vendor login through completion with a scripted model refresh."""
    signed_out = set_account_state(account["id"], "signed_out", expected_revision=get_account(account["id"])["revision"])
    operation = account_controls.SignIn(account["id"], "codex", signed_out["revision"], process=ExitedLogin(),
                                        bind=FirstRunBind(bind, "user") if bind else None)
    broadcasts = []

    async def status(*_args):
        return True, {"account_fp": f"synthetic-{account['id']}"}, None

    async def no_default(_provider):
        return None

    async def discovery(provider, **kwargs):
        return await refresh(provider, **kwargs)

    monkeypatch.setattr(account_controls, "_vendor_status", status)
    monkeypatch.setattr(account_controls, "refresh_default_identity", no_default)
    monkeypatch.setattr(harness_models, "refresh_provider", discovery)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: broadcasts.append("sign-in"))
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda *_a, **_k: broadcasts.append("commit"))
    await account_controls._hold(operation, account["source_home"], "/synthetic/codex")
    return operation, broadcasts


def _refresh_to(outcome, db, account):
    def read():
        from infra.database import get_db

        connection = get_db()
        try:
            _make(outcome, connection, get_account(account["id"], db=connection))
        finally:
            connection.close()

    async def refresh(provider, **_kwargs):
        await asyncio.to_thread(read)
        return AccountRefreshResult(provider, account["id"], status="ready", model_count=1)
    return refresh


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", OUTCOMES)
async def test_a_first_run_sign_in_reports_its_outcome_and_binds_only_when_ready(conductor, monkeypatch, outcome):
    db, _work, personal = conductor

    operation, broadcasts = await _signin(monkeypatch, db, personal, _refresh_to(outcome, db, personal))

    public = operation.public()
    assert (public["status"], public["outcome"], public["bind_reason"]) == ("completed", outcome, None)
    assert _bindings(db) == ({"codex": personal["id"]} if outcome == "ready" else {})
    # The bind's own commit broadcasts; the refresh's commit is not scripted here.
    assert ("commit" in broadcasts) == (outcome in ("ready", "limited"))


@pytest.mark.asyncio
@pytest.mark.parametrize("ending,expected", [
    ("failed", ("unchecked", None)),
    ("raised", ("unchecked", None)),
    ("superseded", ("bind_refused", "stale")),
])
async def test_a_sign_in_whose_refresh_did_not_read_ready_binds_nothing(conductor, monkeypatch, ending, expected):
    db, _work, personal = conductor

    async def refresh(provider, **_kwargs):
        if ending == "raised":
            raise RuntimeError("synthetic discovery failure")
        cause = "account_refresh_superseded" if ending == "superseded" else "synthetic"
        return AccountRefreshResult(provider, personal["id"], status="failed", cause=cause)

    operation, broadcasts = await _signin(monkeypatch, db, personal, refresh)

    assert (operation.public()["outcome"], operation.public()["bind_reason"]) == expected
    assert _bindings(db) == {}
    assert broadcasts == ["sign-in"]


@pytest.mark.asyncio
async def test_a_conductor_started_during_sign_in_reads_bind_refused(conductor, monkeypatch):
    db, _work, personal = conductor
    ready = _refresh_to("ready", db, personal)

    def start():
        from infra.database import get_db

        connection = get_db()
        try:
            connection.execute("UPDATE threads SET model=? WHERE id=?", (MODEL, CONDUCTOR))
            connection.commit()
        finally:
            connection.close()

    async def refresh(provider, **kwargs):
        await asyncio.to_thread(start)
        return await ready(provider, **kwargs)

    operation, _ = await _signin(monkeypatch, db, personal, refresh)

    assert (operation.public()["outcome"], operation.public()["bind_reason"]) == ("bind_refused", "corsia_agent_started")
    assert _bindings(db) == {}


@pytest.mark.asyncio
async def test_a_sign_in_without_bind_thread_id_shows_no_outcome(conductor, monkeypatch):
    db, _work, personal = conductor

    operation, broadcasts = await _signin(monkeypatch, db, personal, _refresh_to("ready", db, personal), bind=None)

    assert "outcome" not in operation.public()
    assert _bindings(db) == {}
    assert broadcasts == []


@pytest.mark.asyncio
async def test_j7_the_personal_account_signed_in_on_the_card_runs_the_first_turn(conductor, monkeypatch):
    """Work is bound; Personal signs in from the card, reads Ready and is bound; the pick keeps it."""
    from connections.account_policy import require_claim

    db, work, personal = conductor
    _discover(db, work)
    assert bind_corsia_agent_account(CONDUCTOR, work["id"], user_id="user")["bound"]
    assert _marked() == [work["id"]]

    operation, _ = await _signin(monkeypatch, db, personal, _refresh_to("ready", db, personal))
    assert operation.public()["outcome"] == "ready"
    assert _marked() == [personal["id"]]

    _pick()

    assert turn_account("codex", thread_id=CONDUCTOR, model=MODEL, db=db) == (personal["id"], None)
    assert require_claim("codex", thread_id=CONDUCTOR, model=MODEL, db=db) == personal["id"]
