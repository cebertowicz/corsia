"""Which account a turn gets, in what order, and what switching it causes.

Pins: FIFO claim order across a provider's accounts, rollover atomicity at startup, account
lifecycle order, the Default's availability and the effects of choosing it, per-account
usage, and the per-account overage switch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from connections import account_rollover, harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import chosen_account_id, select_account
from vendor_account_fixtures import set_legacy_policy
from connections.account_receipts import capture_receipt
from connections.harness_usage import set_allow_overage
from connections.usage_verdicts import LimitSubject
from connections.vendor_accounts import (
    AccountError,
    add_account,
    default_account,
    get_account,
    set_account_state,
)
from infra.database import get_db, write_transaction
from infra.thread_inputs import start_idle_turn
from vendor_account_fixtures import (  # noqa: F401
    _accept,
    _configured,
    _known_default,
    _move,
    _ready,
    _start,
    accounts,
    claude,
    default_id,
    default_receipt,
    default_subject,
    model_default_id,
    ready,
    receipt_of,
    seed_default_account,
    vendors,
)


def _row():
    return {"id": "codex/test", "litellm_model": "codex/test", "connection": "codex",
            "status": "ready", "context_window": 999, "capabilities": {"own": True},
            "resolved_identity": "gpt-6.1-sol", "usage": {"usable": True}}


def _windows():
    now = datetime.now(timezone.utc)
    return [{"vendor": "claude-code", "kind": kind, "used_percent": 100,
             "resets_at": (now + timedelta(hours=2)).isoformat(), "observed_at": now.isoformat(),
             "source": "discovery", "scope_model": scope}
            for kind, scope in (("five_hour", None), ("seven_day_sonnet", "sonnet"))]


def _raw(db, account):
    return db.execute("SELECT usage_json FROM vendor_accounts WHERE id=?", (account,)).fetchone()[0]


def _holds(db):
    return [tuple(row) for row in db.execute("SELECT * FROM thread_vendor_account_holds ORDER BY thread_id")]


async def _switch_on_after_limit_hold(accounts, monkeypatch, order):
    """Hold a queued head on the limited source, then switch rollover on; return what the drive admitted."""
    import asyncio
    from unittest.mock import AsyncMock

    import infra.app_loop as app_loop

    db, first, second, events = _configured(accounts, monkeypatch, enabled=False)
    _accept(db, 'input')
    assert type(_start(db, 'held')).__name__ == 'HeldAdmission'
    admitted = AsyncMock()
    monkeypatch.setattr('infra.thread_execution.drive_turn_admission', admitted)
    app_loop.capture(asyncio.get_running_loop())
    try:
        set_legacy_policy('destination-project', 'codex', allowlist=[first['id'], second['id']] if order is None else order(first, second),
                   default_account_id=first['id'], switch_on_limit=True, db=db)
        for _ in range(3000):  # Up to 30s on a loaded runner; exits at the first admission.
            if admitted.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()
    return db, first, second, events, admitted


def _accept_at_fixed_time(db, identity):
    from infra.input_admission import accept_thread_input

    return accept_thread_input("destination", role="user", content=identity, intent="apply_now",
                               message_id=identity, created_at="2026-09-30T12:00:00", db=db)


def _start_at_fixed_time(db, identity):
    from infra.thread_inputs import start_idle_turn

    return start_idle_turn("destination", turn_id=identity, boot_epoch="test",
                           started_at="2026-09-30T12:00:01", db=db)


def _rows(db):
    return [dict(row) for row in db.execute("SELECT id,content,input_order,seq,delivery_state,target_model,"
                                          "active_turn_id,write_started_at,delivery_error,write_outcome "
                                          "FROM messages WHERE thread_id='destination' ORDER BY input_order")]


def _prefix_ids(db):
    from infra.database import write_transaction
    from infra.thread_inputs import _claimable_prefix

    with write_transaction(db, scope="test_prefix") as connection:
        prefix = _claimable_prefix(connection, "destination")
    return None if prefix is None else [str(row["id"]) for row in prefix[2]]


def _fail_prefix(db):
    from infra.database import write_transaction
    from infra.thread_inputs import fail_claimable_prefix

    with write_transaction(db, scope="test_fail_prefix") as connection:
        return fail_claimable_prefix(connection, "destination", error="usage_unprovable")


def _deny_unprojected(db, second):
    """Deny the bound account without the writer's refresh, so no row holds yet.

    Only the claim-time recheck inside ``_claimable_prefix`` can then hold the
    prefix; the rows' own proved-no-write marks would otherwise stop it first.
    """
    import json

    allowed = second["id"]
    disabled = [row["id"] for row in db.execute(
        "SELECT a.id FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id "
        "WHERE c.provider='codex' AND a.state!='removed'") if row["id"] != allowed]
    db.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES ('destination-project',1)")
    db.execute("INSERT INTO provider_policies(project_id,provider_key,rollover,order_json,disabled_json,revision) "
               "VALUES ('destination-project','codex',0,?, ?,1)",
               (json.dumps([allowed]), json.dumps(disabled)))
    db.commit()
    assert {r["write_outcome"] for r in _rows(db)} == {None}


def _name_project(db, thread_id, project_id):
    db.execute("UPDATE threads SET project_id=? WHERE id=?", (project_id, thread_id))
    db.commit()


_RESET = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


@pytest.fixture
def claude_accounts(isolated_db, monkeypatch):
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, default_account
    from infra.database import get_db

    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", Mock())
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"})
    legacy = seed_default_account("claude-code")
    legacy = set_account_state(legacy['id'], 'signed_in', expected_revision=legacy['revision'])
    second = add_account("claude-code", "Second")
    third = add_account("claude-code", "Third")
    db = get_db()
    receipt = capture_receipt('claude-code', account_id=legacy['id'], db=db)
    assert mark_discovery(legacy['id'], receipt=receipt, status='ready',
                          models={'claude-code/sonnet': {'available': True}}, db=db)
    db.commit()
    yield db, legacy, second, third
    db.close()


def _usage_json(db, account_id: str) -> str | None:
    return db.execute("SELECT usage_json FROM vendor_accounts WHERE id=?", (account_id,)).fetchone()[0]


def _usage(db, account_id: str) -> dict:
    stored = _usage_json(db, account_id)
    assert stored is not None
    return json.loads(stored)


def _sonnet_window() -> dict:
    return {
        "vendor": "claude-code", "kind": "seven_day_sonnet", "used_percent": 100,
        "resets_at": _RESET, "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "rate_limit_event", "scope_model": "sonnet",
    }


RESET = '2100-01-01T00:00:00+00:00'


def _limit(db, account):
    """Hold ``account`` at its plan limit, the verdict 'Spend past usage limit' releases."""
    from connections import harness_usage as usage
    from core.stop_causes import PLAN_LIMIT_REASON

    usage.record_limited(provider='codex', reason=PLAN_LIMIT_REASON, resets_at=RESET, source='test',
                         receipt=_ready(db, account), db=db)


@pytest.fixture
def client(accounts):
    from api.vendor_accounts import router
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {'id': 'user'}
    with TestClient(app) as api:
        yield api


@pytest.mark.parametrize('expired_scope', ['test', None])
def test_due_destination_sibling_scope_holds_without_move_or_receipt(accounts, monkeypatch, expired_scope):
    db, first, second, events = _configured(accounts, monkeypatch)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', second['id'], db=db), scope_model=expired_scope,
        reason='expired destination scope', resets_at='2000-01-01T00:00:00+00:00', source='rate_limit_update', db=db)
    harness_usage.record_allowed(provider='codex', receipt=receipt_of('codex', second['id'], db=db),
        scope_model=None if expired_scope else 'test', source='discovery', db=db)
    usage = harness_usage.project_for_model(harness_usage.stored_usage(provider='codex', account_id=second['id'], db=db), model='codex/test')
    assert usage['state'] == 'allowed' and usage['usable'] is True
    _accept(db, 'stale')
    db.execute("UPDATE messages SET continuation_condition=? WHERE id='stale'",
        ('{"kind":"t0_liveness","subject_id":"missing-source","details":{}}',))
    db.commit()
    _accept(db, 'input')
    source_usage = harness_usage.stored_usage(provider='codex', account_id=first['id'], db=db)
    from infra.held_admission import connection_limit_gate
    source_hold = connection_limit_gate('destination', 'codex/test', hold_expired_at_startup=True, db=db)[1]
    before = db.execute("SELECT runtime_context_epoch FROM threads WHERE id='destination'").fetchone()[0]
    batch = start_idle_turn('destination', turn_id='startup', boot_epoch='test', started_at='t',
        hold_expired_limit_at_startup=True, db=db)
    assert type(batch).__name__ == 'HeldAdmission'
    assert batch == source_hold
    assert (batch.reason, batch.resets_at) == ('usage_limit', '2099-01-01T00:00:00+00:00')
    assert harness_usage.stored_usage(provider='codex', account_id=first['id'], db=db) == source_usage
    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    assert db.execute("SELECT runtime_context_epoch FROM threads WHERE id='destination'").fetchone()[0] == before
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE id='startup'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata,'$.type')='account_switch'").fetchone()[0] == 0
    assert db.execute("SELECT delivery_state,active_turn_id FROM messages WHERE id='input'").fetchone()[:] == ('queued', None)
    assert events == []
    assert db.execute("SELECT delivery_state,delivery_error FROM messages WHERE id='stale'").fetchone()[:] == ('cancelled', 'stale_plan_wake')


@pytest.mark.parametrize('case', ['unready', 'ready'])
def test_actual_startup_claim_preserves_unready_and_commits_ordered_ready_move(accounts, monkeypatch, case):
    db, first, second, events = _configured(accounts, monkeypatch)
    if case == 'unready':
        harness_usage.clear_account_usage('codex', second['id'], db=db)
    _accept(db, 'first')
    _accept(db, 'second')
    batch = start_idle_turn('destination', turn_id='startup', boot_epoch='test', started_at='t',
        hold_expired_limit_at_startup=True, db=db)
    if case == 'unready':
        assert type(batch).__name__ == 'HeldAdmission'
        assert chosen_account_id('destination', 'codex', db=db) == first['id'] and events == []
        assert db.execute("SELECT COUNT(*) FROM active_turns WHERE id='startup'").fetchone()[0] == 0
    else:
        assert type(batch).__name__ == 'ClaimedBatch'
        assert [item.message_id for item in batch.items] == ['first', 'second']
        assert chosen_account_id('destination', 'codex', db=db) == second['id'] and len(events) == 1
        assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='startup'").fetchone()[0] == second['id']


def test_actual_sql_claim_failure_after_tentative_move_rolls_back_receipt_and_effect(accounts, monkeypatch):
    import sqlite3

    db, first, _, events = _configured(accounts, monkeypatch)
    _accept(db, 'input')
    db.execute("CREATE TRIGGER refuse_claim BEFORE INSERT ON active_turns "
        "BEGIN SELECT RAISE(ABORT,'synthetic actual claim refusal'); END")
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='synthetic actual claim refusal'):
        start_idle_turn('destination', turn_id='startup', boot_epoch='test', started_at='t',
            hold_expired_limit_at_startup=True, db=db)
    assert not db.in_transaction and list(db.iterdump()) == before
    assert chosen_account_id('destination', 'codex', db=db) == first['id'] and events == []


def test_default_catalog_models_stay_selectable_after_its_identity_is_observed(accounts):  # noqa: F811
    from connections.vendor_accounts import record_evidence
    from llm.account_catalog import project_accounts

    db, default, _, _ = accounts
    _known_default(db, default)
    receipt = capture_receipt('codex', account_id=default['id'], db=db)
    assert mark_discovery(default['id'], receipt=receipt, status='ready',
                          models={'codex/test': {'context_window': 999, 'resolved_identity': 'gpt-6.1-sol'}}, db=db)
    db.commit()

    before = project_accounts([_row()], project_id="destination-project", db=db)[0]
    before_selected = next(item for item in before["runs_on"] if item["selected"])
    assert before_selected["status"] == "ready"
    assert before["context_window"] == 999

    record_evidence(default['id'], identity={"account_fp": "fp"}, db=db)
    db.commit()

    after = project_accounts([_row()], project_id="destination-project", db=db)[0]
    selected = next(item for item in after["runs_on"] if item["selected"])
    assert selected["status"] == "ready", selected["status_reason"]
    assert selected["status_reason"] is None
    assert selected["account_id"] == default["id"]
    assert after["context_window"] == 999
    assert after["resolved_identity"] == "gpt-6.1-sol"
    assert after["capabilities"] == before["capabilities"]
    assert after["capabilities"]["context_window_max"] == 999


def test_default_claim_requires_its_own_discovery(accounts):  # noqa: F811
    from connections.account_policy import AccountAdmissionError, require_claim

    db, default, _, _ = accounts
    _known_default(db, default)
    db.execute("UPDATE vendor_accounts SET discovery_json=NULL WHERE id=?", (default['id'],))
    db.commit()
    with pytest.raises(AccountAdmissionError) as refused:
        require_claim("codex", thread_id="destination", model="codex/test", db=db)
    assert refused.value.reason == "account_model_not_discovered"


def test_signed_out_default_refuses_a_claim(accounts):  # noqa: F811
    from connections.account_policy import AccountAdmissionError, require_claim

    db, default, _, _ = accounts
    set_account_state(default["id"], "signed_out", expected_revision=default["revision"], db=db)
    with pytest.raises(AccountAdmissionError) as refused:
        require_claim("codex", thread_id="destination", model="codex/test", db=db)
    assert refused.value.reason == "account_signed_out"


def test_custom_exhaustion_and_public_overage_never_drive_default_or_other_account(isolated_db, monkeypatch):
    default = seed_default_account("claude-code")
    x, y = add_account("claude-code", "X"), add_account("claude-code", "Y")
    x = set_account_state(x["id"], "signed_in", expected_revision=x["revision"])
    releases, stops, notifications, probes = [], [], [], []
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", releases.append)
    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", stops.append)
    monkeypatch.setattr("infra.connection_limit_release.apply_plan_limit_effect", lambda *a, **k: stops.append(a))
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda: notifications.append(True))
    monkeypatch.setattr("connections.harness_usage_check.schedule_account_check", probes.append)
    db = get_db()
    try:
        sentinel = {"version": 3, "windows": {}, "facts": {"plan": "untouched"},
                    "vendor_verdicts": {"sonnet": {"state": "limited", "reason": "vendor",
                        "release": {"state": "started", "thread_id": "sentinel"}}}}
        for account in (default, y):
            db.execute("UPDATE vendor_accounts SET usage_json=? WHERE id=?", (json.dumps(sentinel), account["id"]))
        for thread, account in (("d", default), ("x", x), ("y", y)):
            db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES (?,'u','claude-code/sonnet','t0','t0')", (thread,))
            db.execute("INSERT INTO thread_vendor_account_holds VALUES (?,'claude-code',?,'account_runtime_pending','t0')", (thread, account["id"]))
        db.commit()
        before = {account["id"]: _raw(db, account["id"]) for account in (default, y)}
        holds = _holds(db)
        config = db.execute("SELECT config FROM connections WHERE provider='claude-code'").fetchone()[0]
        harness_usage.record_usage(provider="claude-code", receipt=receipt_of("claude-code", x["id"], db=db), windows=_windows(), facts={"plan": "X"}, db=db)
        exhausted = harness_usage.stored_usage(provider="claude-code", account_id=x["id"], db=db)
        assert exhausted["facts"] == {"plan": "X"}
        assert {key: v["state"] for key, v in exhausted["vendor_verdicts"].items()} == {"null": "limited", "sonnet": "limited"}
        set_allow_overage("claude-code", x["id"], True, db=db)
        on = harness_usage.stored_usage(provider="claude-code", account_id=x["id"], db=db)
        assert on["allow_overage"] is True
        assert {key: v["state"] for key, v in on["vendor_verdicts"].items()} == {"null": "allowed", "sonnet": "allowed"}
        set_allow_overage("claude-code", x["id"], False, db=db)
        off = harness_usage.stored_usage(provider="claude-code", account_id=x["id"], db=db)
        assert off["allow_overage"] is False
        assert {key: v["state"] for key, v in off["vendor_verdicts"].items()} == {"null": "limited", "sonnet": "limited"}
        assert {account["id"]: _raw(db, account["id"]) for account in (default, y)} == before
        assert _holds(db) == holds
        assert db.execute("SELECT config FROM connections WHERE provider='claude-code'").fetchone()[0] == config
        assert releases == stops == probes == []
        assert notifications == [True, True, True, True]
    finally:
        db.close()


def test_default_keeps_multiscope_effects_after_commit(isolated_db, monkeypatch):
    default = seed_default_account("claude-code")
    default = set_account_state(default['id'], 'signed_in', expected_revision=default['revision'])
    releases, notifications, plan_effects = [], [], []
    db = get_db()
    receipt = capture_receipt('claude-code', account_id=default['id'], db=db)
    assert mark_discovery(default['id'], receipt=receipt, status='ready',
                          models={'claude-code/sonnet': {'available': True}}, db=db)
    db.commit()
    def release(subject):
        assert not db.in_transaction
        releases.append((subject.provider, subject.scope_key))
    def notify():
        assert not db.in_transaction
        notifications.append(True)
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", release)
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", notify)
    monkeypatch.setattr("infra.connection_limit_release.apply_plan_limit_effect", lambda provider, scopes, **kw: plan_effects.append((provider, [scope for scope, _ in scopes], kw["db"].in_transaction)))
    try:
        with write_transaction(db, scope="proof_default_exhaustion"):
            harness_usage.record_usage(provider="claude-code", windows=_windows(), db=db, receipt=default_receipt("claude-code", db=db))
            assert notifications == releases == []
            assert plan_effects == [("claude-code", [None, "sonnet"], True)]
        assert notifications == [True]
        with write_transaction(db, scope="proof_default_overage"):
            set_allow_overage("claude-code", default["id"], True, db=db)
            assert releases == []
        assert releases == [("claude-code", "null"), ("claude-code", "sonnet")]
        assert notifications == [True, True, True]
        set_allow_overage("claude-code", default["id"], False, db=db)
        assert plan_effects == [("claude-code", [None, "sonnet"], True), ("claude-code", [None, "sonnet"], True)]
        assert notifications == [True, True, True, True]
    finally:
        db.close()


def test_proven_limit_moves_then_claims_exact_fifo_and_preserves_source_usage(accounts, monkeypatch):
    from infra.vendor_login import read_turn_receipt

    db, first, second, events = _configured(accounts, monkeypatch)
    before = harness_usage.stored_usage(provider='codex', account_id=first['id'], db=db)
    _accept(db, 'first')
    _accept(db, 'second')
    batch = _start(db, 'new-turn')
    assert batch is not None and [item.message_id for item in batch.items] == ['first', 'second']
    assert chosen_account_id('destination', 'codex', db=db) == second['id']
    assert db.execute("SELECT vendor_account_id,model FROM active_turns WHERE id='new-turn'").fetchone()[:] == (second['id'], 'codex/test')
    assert read_turn_receipt('new-turn', db=db) == capture_receipt('codex', account_id=second['id'], db=db)
    assert harness_usage.stored_usage(provider='codex', account_id=first['id'], db=db) == before
    assert len(events) == 1
    receipt = db.execute("SELECT role,content,metadata,delivery_state FROM messages WHERE id=?", (events[0]['message_id'],)).fetchone()
    assert (receipt['role'], receipt['content'], receipt['delivery_state']) == ('system', 'Moved to Codex · Third', 'delivered')
    metadata = json.loads(receipt['metadata'])
    assert metadata['from_account_id'] == first['id'] and metadata['to_account_id'] == second['id']
    assert metadata['type'] == 'account_switch' and metadata['reason'] == 'account_limit'
    assert not {'login_seq', 'account_revision', 'source_home', 'runtime_home', 'receipt'} & set(metadata)


def _passive_digest(db):
    from infra.input_admission import accept_thread_input

    return accept_thread_input(
        'destination', role='system', content='[PASSIVE CONTEXT DIGEST]\n1 update',
        intent='queue_after_turn', message_id='passive',
        metadata={'type': 'context_event', 'event_type': 'context.digest', 'event_disposition': 'passive'},
        db=db,
    )


def test_rollover_lifts_passive_and_user_account_markers_and_preserves_fifo(accounts, monkeypatch):
    db, first, second, events = _configured(accounts, monkeypatch)
    _passive_digest(db)
    _accept(db, 'input')
    _accept(db, 'later')
    db.execute("UPDATE messages SET write_outcome='proved_no_write',delivery_error='account_model_not_discovered' "
               "WHERE id IN ('passive','input')")
    db.execute("UPDATE messages SET write_outcome='proved_no_write',delivery_error='other' WHERE id='later'")
    db.commit()

    batch = _start(db, 'new-turn')

    assert [item.message_id for item in batch.items] == ['passive', 'input']
    assert chosen_account_id('destination', 'codex', db=db) == second['id']
    assert len(events) == 1
    assert db.execute("SELECT input_order FROM messages WHERE id=?", (events[0]['message_id'],)).fetchone()[0] is None
    assert [tuple(row) for row in db.execute(
        "SELECT id,write_outcome,delivery_error FROM messages "
        "WHERE id IN ('passive','input','later') ORDER BY input_order")
    ] == [('passive', None, None), ('input', None, None), ('later', 'proved_no_write', 'other')]


def test_unlifted_head_hold_preserves_queue_and_prevents_rollover(accounts, monkeypatch):
    db, first, _, events = _configured(accounts, monkeypatch)
    _accept(db, 'held')
    _accept(db, 'input')
    db.execute("UPDATE messages SET write_outcome='proved_no_write',delivery_error='other' WHERE id='held'")
    db.commit()
    before = [tuple(row) for row in db.execute(
        "SELECT id,input_order,write_outcome FROM messages WHERE id IN ('held','input') ORDER BY input_order")]

    assert _start(db, 'new-turn') is None

    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    assert events == []
    assert [tuple(row) for row in db.execute(
        "SELECT id,input_order,write_outcome FROM messages WHERE id IN ('held','input') ORDER BY input_order")] == before


def test_passive_only_eligible_prefix_never_rolls_over(accounts, monkeypatch):
    db, first, _, events = _configured(accounts, monkeypatch)
    _passive_digest(db)

    assert _start(db, 'new-turn') is None
    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    assert events == []


def test_written_nonhead_in_eligible_prefix_blocks_rollover(accounts, monkeypatch):
    db, first, _, events = _configured(accounts, monkeypatch)
    _accept(db, 'head')
    _accept(db, 'written')
    db.execute("UPDATE messages SET write_started_at='t' WHERE id='written'")
    db.commit()

    assert type(_start(db, 'new-turn')).__name__ == 'HeldAdmission'

    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    assert events == []


@pytest.mark.parametrize('case', ['off', 'unknown-target', 'signed-out', 'no-other-account', 'ambiguous'])
def test_ordinary_holds_do_not_switch_from_uncertainty_or_invalid_target(accounts, monkeypatch, case):
    db, first, second, events = _configured(accounts, monkeypatch, enabled=case != 'off')
    if case == 'unknown-target':
        harness_usage.clear_account_usage('codex', second['id'], db=db)
    elif case == 'signed-out':
        set_account_state(second['id'], 'signed_out', expected_revision=second['revision'], db=db)
    elif case == 'no-other-account':
        set_legacy_policy('destination-project', 'codex', allowlist=[first['id']],
            default_account_id=first['id'], db=db)
    _accept(db, 'input')
    if case == 'ambiguous':
        db.execute("UPDATE messages SET write_outcome='ambiguous' WHERE id='input'")
        db.commit()
    batch = _start(db, 'held')
    assert batch is None or type(batch).__name__ == 'HeldAdmission'
    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    assert events == []
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata,'$.type')='account_switch'").fetchone()[0] == 0


def test_rollback_publishes_nothing_and_repeated_observation_cannot_cycle(accounts, monkeypatch):
    db, first, second, events = _configured(accounts, monkeypatch)
    _accept(db, 'input')
    with pytest.raises(RuntimeError):
        with write_transaction(db, scope='outer_refusal'):
            assert _move(db) is not None
            raise RuntimeError('refused')
    assert events == [] and chosen_account_id('destination', 'codex', db=db) == first['id']
    with write_transaction(db, scope='committed_move'):
        assert _move(db) is not None
    assert len(events) == 1
    from connections.account_policy import AccountAdmissionError

    with pytest.raises(AccountAdmissionError, match='account_change_requires_idle'):
        select_account('destination', 'codex', account_id=first['id'], db=db)
    # Defensive stale-binding injection; public queued rebind remains refused.
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='destination' AND provider='codex'", (first['id'],))
    db.commit()
    with write_transaction(db, scope='same_observation'):
        assert _move(db) is None
    assert len(events) == 1


@pytest.mark.asyncio
async def test_late_receipt_broadcast_keeps_history_but_does_not_overwrite_current_binding(accounts, monkeypatch):
    from thread import model_runtime

    db, first, second, events = _configured(accounts, monkeypatch)
    _accept(db, 'input')
    with write_transaction(db, scope='committed_move'):
        _move(db)
    notice = events[0]
    db.execute("UPDATE threads SET runtime_context_epoch=runtime_context_epoch+1 WHERE id='destination'")
    db.commit()
    history, updates = [], []
    async def breadcrumb(thread_id, message):
        history.append(message['id'])
    monkeypatch.setattr(model_runtime, '_broadcast_model_switch_breadcrumb', breadcrumb)
    monkeypatch.setattr(model_runtime, '_broadcast_model_selection', lambda *a, **kw: updates.append(kw))
    await model_runtime.post_account_switch_effects(notice)
    assert history == [notice['message_id']] and updates == []


@pytest.mark.parametrize('case', ['archived', 'old-model', 'old-epoch', 'started', 'attempted', 'active'])
def test_canonical_nonclaimable_head_never_commits_move(accounts, monkeypatch, case):
    db, first, _, events = _configured(accounts, monkeypatch)
    _accept(db, 'input')
    if case == 'archived':
        db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='destination'")
    elif case == 'old-model':
        db.execute("UPDATE messages SET target_model='codex/old' WHERE id='input'")
    elif case == 'old-epoch':
        db.execute("UPDATE messages SET target_execution_epoch=99 WHERE id='input'")
    elif case == 'started':
        db.execute("UPDATE messages SET write_started_at='t' WHERE id='input'")
    elif case == 'attempted':
        db.execute("UPDATE messages SET delivery_attempt_id='attempt' WHERE id='input'")
    else:
        db.execute("INSERT INTO active_turns(id,thread_id,model,status,started_at,boot_epoch,execution_epoch,vendor_account_id) "
            "VALUES ('old','destination','codex/test','running','t','test',0,?)", (first['id'],))
    db.commit()
    _start(db, 'new')
    assert chosen_account_id('destination', 'codex', db=db) == first['id'] and events == []
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE id='new'").fetchone()[0] == 0


def test_startup_expired_default_cannot_hold_new_custom_fifo_destination(accounts, monkeypatch):
    from infra.thread_inputs import start_idle_turn

    db, first, second, events = _configured(accounts, monkeypatch)
    harness_usage.record_limited(provider='codex', reason='default expired', resets_at='2000-01-01T00:00:00+00:00',
        source='rate_limit_update', db=db, receipt=default_receipt('codex', db=db))
    _accept(db, 'input')
    batch = start_idle_turn('destination', turn_id='startup', boot_epoch='test', started_at='t',
        hold_expired_limit_at_startup=True, db=db)
    assert type(batch).__name__ == 'ClaimedBatch'
    assert [item.message_id for item in batch.items] == ['input']
    assert chosen_account_id('destination', 'codex', db=db) == second['id'] and len(events) == 1
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='startup'").fetchone()[0] == second['id']


@pytest.mark.parametrize('successor', [False, True])
def test_stale_plan_head_is_cancelled_before_any_rollover_and_valid_successor_can_claim(accounts, monkeypatch, successor):
    db, first, second, events = _configured(accounts, monkeypatch)
    _accept(db, 'stale')
    db.execute("UPDATE messages SET continuation_condition=? WHERE id='stale'",
        (json.dumps({'kind': 't0_liveness', 'subject_id': 'missing-source', 'details': {}}),))
    db.commit()
    if successor:
        _accept(db, 'valid')
    batch = _start(db, 'new')
    assert db.execute("SELECT delivery_state,delivery_error FROM messages WHERE id='stale'").fetchone()[:] == ('cancelled', 'stale_plan_wake')
    if successor:
        assert [item.message_id for item in batch.items] == ['valid']
        assert chosen_account_id('destination', 'codex', db=db) == second['id'] and len(events) == 1
    else:
        assert batch is None
        assert chosen_account_id('destination', 'codex', db=db) == first['id'] and events == []


async def test_switching_on_drives_a_head_already_held_on_a_usage_limit(accounts, monkeypatch):
    db, first, second, events, admitted = await _switch_on_after_limit_hold(accounts, monkeypatch, None)
    assert admitted.await_count == 1
    batch = admitted.await_args.args[0]
    assert type(batch).__name__ == 'ClaimedBatch' and [item.message_id for item in batch.items] == ['input']
    assert chosen_account_id('destination', 'codex', db=db) == second['id'] and len(events) == 1
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?", (batch.turn_id,)).fetchone()[0] == second['id']


async def test_switching_on_without_an_eligible_destination_keeps_the_input_held(accounts, monkeypatch):
    db, first, second, events, admitted = await _switch_on_after_limit_hold(
        accounts, monkeypatch, lambda source, destination: [source['id']])
    assert [type(call.args[0]).__name__ for call in admitted.await_args_list] == ['HeldAdmission']
    assert chosen_account_id('destination', 'codex', db=db) == first['id'] and events == []
    assert db.execute("SELECT delivery_state,delivery_error,active_turn_id FROM messages WHERE id='input'").fetchone()[:] == ('queued', None, None)
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0


@pytest.mark.parametrize('rollover', [False, True])
def test_pin_moves_an_unwritten_limit_hold_and_claims_on_destination(accounts, monkeypatch, rollover):
    from connections.account_selection import rebind_idle_account

    db, first, second, events = _configured(accounts, monkeypatch, enabled=False)
    _accept(db, 'input')
    assert type(_start(db, 'held')).__name__ == 'HeldAdmission'
    if rollover:
        set_legacy_policy('destination-project', 'codex', allowlist=[first['id'], second['id']],
                          default_account_id=first['id'], switch_on_limit=True, db=db)
    moved = rebind_idle_account('destination', second['id'], expected_runtime_epoch=0, user_id='user', db=db)
    assert moved == {'account_id': second['id'], 'runtime_context_epoch': 1, 'changed': True}
    switches = db.execute("SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchall()
    assert len(switches) == 1 and json.loads(switches[0]['metadata'])['reason'] == 'account_pinned'
    batch = _start(db, 'destination-turn')
    assert batch is not None
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='destination-turn'").fetchone()[0] == second['id']


async def test_pin_move_drives_an_unwritten_limit_held_head(accounts, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    import infra.app_loop as app_loop
    from connections.account_selection import rebind_idle_account

    db, first, second, events = _configured(accounts, monkeypatch, enabled=False)
    _accept(db, 'input')
    assert type(_start(db, 'held')).__name__ == 'HeldAdmission'
    admitted = AsyncMock()
    monkeypatch.setattr('infra.thread_execution.drive_turn_admission', admitted)
    app_loop.capture(asyncio.get_running_loop())
    try:
        moved = rebind_idle_account('destination', second['id'], expected_runtime_epoch=0, user_id='user', db=db)
        for _ in range(500):
            if admitted.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()

    assert moved == {'account_id': second['id'], 'runtime_context_epoch': 1, 'changed': True}
    batch = admitted.await_args.args[0]
    assert type(batch).__name__ == 'ClaimedBatch' and [item.message_id for item in batch.items] == ['input']
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?", (batch.turn_id,)).fetchone()[0] == second['id']


@pytest.mark.parametrize("state,reason", [("signed_out", "account_signed_out"), ("removed", "account_removed")])
def test_lifecycle_keeps_binding_model_inputs_and_active_spend(accounts, state, reason):
    from connections.account_lifecycle import change_state
    from connections.account_policy import select_account

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    _accept_at_fixed_time(db, "first")
    batch = _start_at_fixed_time(db, "running")
    assert batch is not None
    _accept_at_fixed_time(db, "second")
    before = _rows(db)
    change_state(legacy["id"], state, expected_revision=get_account(legacy["id"], db=db)["revision"], db=db)
    after = _rows(db)
    assert [(r["id"], r["content"], r["input_order"], r["seq"], r["target_model"], r["delivery_state"])
            for r in after] == [(r["id"], r["content"], r["input_order"], r["seq"], r["target_model"], r["delivery_state"])
                               for r in before]
    assert after[0]["active_turn_id"] == "running"
    assert after[0]["delivery_error"] == before[0]["delivery_error"]
    assert after[1]["delivery_error"] == reason
    assert after[1]["write_outcome"] == "proved_no_write"
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='running'").fetchone()[0] == legacy["id"]
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='destination'").fetchone()[0] == legacy["id"]
    assert db.execute("SELECT model FROM threads WHERE id='destination'").fetchone()[0] == "codex/test"
    assert db.execute("SELECT reason FROM thread_vendor_account_holds WHERE thread_id='destination'").fetchone()[0] == reason


def test_policy_hold_releases_same_binding_in_original_fifo_order(accounts):
    from connections.account_policy import select_account, set_policy

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]], default_account_id=second["id"],
               db=db)
    _accept_at_fixed_time(db, "first")
    _accept_at_fixed_time(db, "second")
    assert _start_at_fixed_time(db, "blocked") is None
    assert [(r["input_order"], r["delivery_state"], r["seq"], r["delivery_error"]) for r in _rows(db)] == [
        (1, "queued", None, "account_project_denied"), (2, "queued", None, "account_project_denied")]
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"], legacy["id"]],
               default_account_id=second["id"], db=db)
    batch = _start_at_fixed_time(db, "restored")
    assert [item.message_id for item in batch.items] == ["first", "second"]
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='restored'").fetchone()[0] == legacy["id"]
    assert all(r["seq"] is None and r["delivery_error"] is None for r in _rows(db))


def test_policy_hold_ends_the_shared_prefix_for_claim_and_fail_closed(accounts):
    from connections.account_policy import select_account

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    for identity in ("first", "second", "third"):
        _accept_at_fixed_time(db, identity)
    _deny_unprojected(db, second)
    assert _prefix_ids(db) is None
    held = _rows(db)

    assert _fail_prefix(db) is None
    assert _start_at_fixed_time(db, "blocked") is None
    assert _rows(db) == held
    assert {r["delivery_state"] for r in held} == {"queued"}
    assert {r["delivery_error"] for r in held} == {"account_project_denied"}
    assert db.execute("SELECT reason FROM thread_vendor_account_holds WHERE thread_id='destination'"
                      ).fetchone()[0] == "account_project_denied"
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0


def test_eligible_unprovable_prefix_still_fails_closed_once_the_hold_lifts(accounts):
    from connections.account_policy import select_account, set_policy

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    _accept_at_fixed_time(db, "first")
    _accept_at_fixed_time(db, "second")
    _deny_unprojected(db, second)
    assert _fail_prefix(db) is None
    assert {r["delivery_state"] for r in _rows(db)} == {"queued"}
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"], legacy["id"]],
               default_account_id=second["id"], db=db)

    failed = _fail_prefix(db)

    assert failed is not None and list(failed.message_ids) == ["first", "second"]
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_account_holds WHERE thread_id='destination'"
                      ).fetchone()[0] == 0


def test_lifting_the_hold_wakes_the_same_prefix_exactly_once(accounts):
    from connections.account_policy import select_account, set_policy

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    _accept_at_fixed_time(db, "first")
    _accept_at_fixed_time(db, "second")
    _deny_unprojected(db, second)
    assert _start_at_fixed_time(db, "blocked") is None
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"], legacy["id"]],
               default_account_id=second["id"], db=db)

    batch = _start_at_fixed_time(db, "woken")

    assert [item.message_id for item in batch.items] == ["first", "second"]
    assert _start_at_fixed_time(db, "duplicate") is None
    assert [r[0] for r in db.execute("SELECT id FROM active_turns")] == ["woken"]
    assert {r["active_turn_id"] for r in _rows(db)} == {"woken"}


@pytest.mark.parametrize("caller", ["start", "fail"])
def test_claim_and_fail_closed_take_the_identical_prefix(accounts, caller):
    from connections.account_policy import select_account

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    for identity in ("first", "second", "third"):
        _accept_at_fixed_time(db, identity)
    expected = _prefix_ids(db)
    assert expected == ["first", "second", "third"]

    if caller == "start":
        taken = [item.message_id for item in _start_at_fixed_time(db, "claimed").items]
    else:
        taken = list(_fail_prefix(db).message_ids)

    assert taken == expected


def test_dangling_project_holds_instead_of_spending_until_it_resolves(accounts):
    from connections.account_policy import AccountAdmissionError, require_launch, select_account

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    _accept_at_fixed_time(db, "first")
    _name_project(db, "destination", "deleted-project")

    assert _start_at_fixed_time(db, "blocked") is None
    assert [(r["delivery_state"], r["write_outcome"], r["delivery_error"]) for r in _rows(db)] == [
        ("queued", "proved_no_write", "project_policy_missing")]
    assert db.execute("SELECT reason FROM thread_vendor_account_holds WHERE thread_id='destination'"
                      ).fetchone()[0] == "project_policy_missing"
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    with pytest.raises(AccountAdmissionError) as destination:
        require_launch("codex", thread_id="destination", db=db)
    assert destination.value.reason == "project_policy_missing"

    _name_project(db, "destination", "destination-project")
    batch = _start_at_fixed_time(db, "restored")

    assert [item.message_id for item in batch.items] == ["first"]
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='restored'").fetchone()[0] == legacy["id"]


def test_dangling_actor_project_refuses_the_paid_write(accounts):
    from connections.account_policy import AccountAdmissionError, require_launch, select_account

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    _name_project(db, "actor", "deleted-project")

    with pytest.raises(AccountAdmissionError) as actor:
        require_launch("codex", thread_id="destination", actor_thread_id="actor", db=db)
    assert actor.value.reason == "project_policy_missing"


def test_projectless_thread_admission_is_unchanged(accounts):
    from connections.account_policy import require_claim, select_account

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    _name_project(db, "destination", None)
    assert select_account("destination", "codex", db=db) == legacy["id"]
    _accept_at_fixed_time(db, "first")

    assert require_claim("codex", thread_id="destination", model="codex/test", db=db) == legacy["id"]
    assert [item.message_id for item in _start_at_fixed_time(db, "claimed").items] == ["first"]
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_account_holds").fetchone()[0] == 0


@pytest.mark.parametrize("after_fence", [False, True])
def test_policy_race_refusal_never_fails_or_sequences_input(accounts, after_fence):
    from connections.account_policy import AccountAdmissionError, require_launch, select_account, set_policy
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_writes import settle_unwritten_turn_inputs
    from infra.database import write_transaction

    db, legacy, second, third = accounts
    _known_default(db, legacy)
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    _accept_at_fixed_time(db, "first")
    _accept_at_fixed_time(db, "second")
    batch = _start_at_fixed_time(db, "race")
    if after_fence:
        begin_batch_write(batch, attempt_id="write", started_at="t", db=db)
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]], default_account_id=second["id"],
               db=db)
    calls = []
    with pytest.raises(AccountAdmissionError) as rejected:
        if after_fence:
            require_launch("codex", thread_id="destination", turn_id="race", db=db)
        else:
            begin_batch_write(batch, attempt_id="write", started_at="t", db=db)
        calls.append("bytes")
    if after_fence:
        assert settle_batch_write("write", outcome="proved_no_write", error=rejected.value, db=db) == ()
    else:
        with write_transaction(db, scope="test_account_terminal"):
            assert settle_unwritten_turn_inputs(db, "race", outcome="error", error=rejected.value) == ()
    assert calls == []
    assert [(r["id"], r["input_order"], r["seq"], r["delivery_state"], r["active_turn_id"],
             r["write_started_at"], r["write_outcome"], r["delivery_error"]) for r in _rows(db)] == [
        ("first", 1, None, "queued", None, None, "proved_no_write", "account_project_denied"),
        ("second", 2, None, "queued", None, None, "proved_no_write", "account_project_denied")]
    assert db.execute("SELECT next_message_seq FROM threads WHERE id='destination'").fetchone()[0] == 1
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='race'").fetchone()[0] == legacy["id"]


def test_switch_writes_only_the_named_account(claude_accounts) -> None:
    db, legacy, second, third = claude_accounts
    harness_usage.record_usage(provider="claude-code", windows=[_sonnet_window()], db=db, receipt=default_receipt("claude-code", db=db))
    harness_usage.record_usage(provider="claude-code", windows=[_sonnet_window()], receipt=receipt_of("claude-code", third["id"], db=db), db=db)
    untouched = {account["id"]: _usage_json(db, account["id"]) for account in (legacy, third)}

    harness_usage.set_allow_overage("claude-code", second["id"], True, db=db)

    assert _usage(db, second["id"])["allow_overage"] is True
    assert {account["id"]: _usage_json(db, account["id"]) for account in (legacy, third)} == untouched
    assert harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)).get("allow_overage") is not True


def test_unknown_account_is_refused_and_stores_nothing(claude_accounts) -> None:
    db, legacy, second, third = claude_accounts
    before = [_usage_json(db, account["id"]) for account in (legacy, second, third)]
    with pytest.raises(AccountError, match="account does not exist"):
        harness_usage.set_allow_overage("claude-code", "missing", True, db=db)
    assert [_usage_json(db, account["id"]) for account in (legacy, second, third)] == before


def test_default_account_switch_still_releases_its_held_plan_scope(claude_accounts, monkeypatch) -> None:
    from connections.harness_usage import set_allow_overage

    db, legacy, second, third = claude_accounts
    released = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", released)
    harness_usage.record_usage(provider="claude-code", windows=[_sonnet_window()], db=db, receipt=default_receipt("claude-code", db=db))
    assert harness_usage.connection_availability("claude-code/sonnet", db=db, account_id=model_default_id("claude-code/sonnet", db=db)).state == "limited_with_reset"
    second_before = _usage_json(db, second["id"])

    set_allow_overage("claude-code", legacy["id"], True, db=db)

    assert released.call_args.args == (default_subject("claude-code", "sonnet", db=db),)
    assert _usage(db, legacy["id"])["allow_overage"] is True
    assert _usage_json(db, second["id"]) == second_before


@pytest.mark.asyncio
async def test_every_limited_account_is_rechecked_on_its_own_login(accounts, monkeypatch):
    from connections import harness_usage_check as checks

    db, default, limited, ready = accounts
    _limit(db, limited)
    _ready(db, ready)
    scheduled = []
    monkeypatch.setattr(checks, '_schedule', lambda provider, account_id: scheduled.append((provider, account_id)))

    await checks.schedule_limited_usage_rechecks()

    assert scheduled == [('codex', limited['id'])]


@pytest.mark.asyncio
async def test_expired_catalog_windows_schedule_their_own_claude_and_codex_reads(vendors, monkeypatch):
    """A picker refresh must repair expired visible meters on both account types."""
    from connections import harness_usage_check as checks

    db, by_provider = vendors
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    claude = by_provider["claude-code"]
    from connections import claude_usage

    claude_usage.apply_usage_response({"rate_limits": {
        "five_hour": {"utilization": 42, "resets_at": expired.isoformat(), "status": "allowed"},
        "seven_day": {"utilization": 46, "resets_at": expired.isoformat(), "status": "allowed"},
    }}, receipt=capture_receipt("claude-code", account_id=claude["id"], db=db), db=db)
    # Claude's live event has no utilization: it must leave control ownership to the bounded recheck.
    assert claude_usage.apply_rate_limit_event(
        {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": expired.timestamp()},
        receipt=capture_receipt("claude-code", account_id=claude["id"], db=db), db=db,
    ) is True
    codex = by_provider["codex"]
    harness_usage.record_rate_limits_read({"ordinaryUsageAllowed": True, "rateLimits": {
        "limitId": "codex", "primary": {"usedPercent": 42, "resetsAt": int(expired.timestamp()), "windowDurationMins": 300},
    }}, provider="codex", receipt=capture_receipt("codex", account_id=codex["id"], db=db), db=db)
    scheduled = []
    monkeypatch.setattr(checks, "_schedule", lambda provider, account_id: scheduled.append((provider, account_id)))

    await checks.schedule_limited_usage_rechecks()

    assert scheduled == [("codex", by_provider["codex"]["id"]), ("claude-code", by_provider["claude-code"]["id"])]


def test_spend_past_plan_limit_is_set_on_one_account_and_releases_only_it(client, accounts, monkeypatch):
    from connections import harness_usage as usage

    monkeypatch.setattr('infra.state.ws.notify_models_changed', lambda: None)
    db, default, chosen, other = accounts
    _limit(db, chosen)
    _limit(db, other)

    response = client.put(f"/api/vendor-accounts/{chosen['id']}/usage", json={'allow_overage': True})

    assert response.status_code == 200
    assert response.json()['allow_overage'] is True
    shown = {row['id']: row['allow_overage'] for row in client.get('/api/vendor-accounts?provider=codex').json()['accounts']}
    assert shown == {default['id']: False, chosen['id']: True, other['id']: False}
    assert usage.project_for_model(usage.stored_usage(provider='codex', account_id=chosen['id'], db=db))['usable'] is True
    assert usage.project_for_model(usage.stored_usage(provider='codex', account_id=other['id'], db=db))['usable'] is False


def test_a_refused_usage_write_is_logged_with_its_account(accounts, caplog):
    from connections import harness_usage as usage
    from connections.vendor_accounts import set_account_state

    db, _default, account, _ = accounts
    old = _ready(db, account)
    set_account_state(account['id'], 'signed_in', expected_revision=account['revision'], db=db)
    before = usage.stored_usage(provider='codex', account_id=account['id'], db=db)

    with caplog.at_level(logging.WARNING, logger='connections.account_receipts'):
        usage.record_usage(provider='codex', facts={'planType': 'late'}, receipt=old, db=db)
        usage.record_usage(provider='codex', facts={'planType': 'lost'}, receipt=None, db=db)

    assert usage.stored_usage(provider='codex', account_id=account['id'], db=db) == before
    assert [record.getMessage() for record in caplog.records] == [
        f"codex account write refused: login receipt for account {account['id']} is stale",
        'codex account write refused: login receipt for account None is missing',
    ]


@pytest.mark.asyncio
async def test_a_claude_rate_limit_event_lands_on_the_sessions_own_account(vendors):
    from connections import harness_usage as usage
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from subagents.cc_runtime_events import _on_rate_limit
    from vendor_account_fixtures import default_id

    db, by_provider = vendors
    account = by_provider['claude-code']
    receipt = capture_receipt('claude-code', account_id=account['id'], db=db)
    assert receipt is not None
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={'claude-code/test': {}}, db=db)
    db.commit()
    seed_default_account('claude-code', db=db)
    default = default_id('claude-code', db=db)
    session = SimpleNamespace(vendor_account_bound=True, vendor_account_receipt=receipt)
    event = {'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': 4102444800}}

    await _on_rate_limit(SimpleNamespace(live=SimpleNamespace(session=session)), event)

    limited = usage.stored_usage(provider='claude-code', account_id=account['id'], db=db)
    assert usage.project_for_model(limited)['usable'] is False
    assert usage.project_for_model(usage.stored_usage(provider='claude-code', account_id=default, db=db))['usable'] is True


def test_switch_onto_a_harness_model_rolls_over_from_the_model_being_left(accounts, monkeypatch):
    """A switch must read the destination's harness, not the model it is leaving.

    An added Token-Plan account's model carries no harness. Switching onto a harness
    model from one still has to move the thread's harness binding to the rollover
    destination; it must not refuse with account_requires_vendor_model.
    """
    from infra.model_switches import request_switch
    from infra.vendor_turn_binding import model_vendor

    db, first, second, _events = _configured(accounts, monkeypatch)
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('token-plan/plan','Plan','token-plan/plan','token-plan','plan','native','t','t')")
    db.execute("UPDATE threads SET model='token-plan/plan' WHERE id='destination'")
    db.commit()
    assert model_vendor('token-plan/plan', db=db) is None
    assert model_vendor('codex/test', db=db) == 'codex'

    request_switch('destination', 'codex/test', operation_id='op-switch', requested_at='t',
                   user_id='user', db=db)

    assert chosen_account_id('destination', 'codex', db=db) == second['id']
    assert db.execute("SELECT pending_model FROM threads WHERE id='destination'").fetchone()[0] == 'codex/test'
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == 0


def test_model_switch_rollover_moves_only_the_destination_provider_binding(accounts, monkeypatch):
    """A cross-harness switch must not derive the source provider from threads.model."""
    from connections.account_selection import bind_model_account
    from connections.models import upsert_connection
    db, first, second, _events = _configured(accounts, monkeypatch)
    upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/test'})
    claude = seed_default_account('claude-code', db=db)
    from connections.vendor_accounts import set_account_state
    claude = set_account_state(claude['id'], 'signed_in', expected_revision=claude['revision'], db=db)
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('claude-code/test','Claude','claude-code/test','claude-code','test','claude-code','t','t')")
    db.execute("UPDATE threads SET model='claude-code/test' WHERE id='destination'")
    db.commit()
    select_account('destination', 'claude-code', account_id=claude['id'], db=db)

    assert bind_model_account('destination', 'codex/test', db=db) == second['id']
    bindings = dict(db.execute("SELECT provider,account_id FROM thread_vendor_accounts WHERE thread_id='destination'"))
    assert bindings == {'claude-code': claude['id'], 'codex': second['id']}
    switch = db.execute("SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()
    assert json.loads(switch['metadata'])['reason'] == 'account_limit'


@pytest.mark.parametrize('bound,limited', [('second', False), ('first', True)])
def test_first_pick_on_a_model_less_thread_keeps_a_usable_binding_and_moves_a_limited_one(
        accounts, monkeypatch, bound, limited):
    """With rollover on, a model-less thread's first pick keeps the account it is bound to
    when that account can run the model; only a limited binding moves to the destination."""
    from connections.account_selection import bind_model_account

    db, first, second, _events = _configured(accounts, monkeypatch)
    db.execute("DELETE FROM thread_vendor_accounts WHERE thread_id='destination'")
    db.execute("UPDATE threads SET model=NULL WHERE id='destination'")
    db.commit()
    account = {'first': first, 'second': second}[bound]
    select_account('destination', 'codex', account_id=account['id'], db=db)

    assert bind_model_account('destination', 'codex/test', db=db) == second['id']
    switches = [json.loads(row['metadata'])['reason'] for row in db.execute(
        "SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'")]
    assert switches == (['account_limit'] if limited else [])


@pytest.mark.parametrize('state', ['archived', 'paused'])
def test_model_binding_and_pin_share_the_movable_thread_rule(accounts, monkeypatch, state):
    from connections.account_policy import AccountAdmissionError
    from connections.account_selection import bind_model_account, rebind_idle_account

    db, first, second, _events = _configured(accounts, monkeypatch)
    db.execute("UPDATE threads SET lifecycle_state=? WHERE id='destination'", (state,))
    db.commit()
    assert bind_model_account('destination', 'codex/test', db=db) == first['id']
    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    with pytest.raises(AccountAdmissionError, match='account_change_requires_idle'):
        rebind_idle_account('destination', second['id'], expected_runtime_epoch=0, user_id='user', db=db)


@pytest.mark.parametrize('column,value,blocked', [
    ('write_started_at', 'started', True),
    ('delivery_attempt_id', 'attempt', True),
    ('write_started_at', None, False),
])
def test_model_binding_and_pin_share_the_queued_write_rule(accounts, monkeypatch, column, value, blocked):
    from connections.account_policy import AccountAdmissionError
    from connections.account_selection import bind_model_account, rebind_idle_account

    db, first, second, _events = _configured(accounts, monkeypatch)
    _accept(db, 'input')
    db.execute(f'UPDATE messages SET {column}=? WHERE id=\'input\'', (value,))
    db.commit()
    if blocked:
        assert bind_model_account('destination', 'codex/test', db=db) == first['id']
        assert chosen_account_id('destination', 'codex', db=db) == first['id']
        with pytest.raises(AccountAdmissionError, match='account_change_requires_idle'):
            rebind_idle_account('destination', second['id'], expected_runtime_epoch=0, user_id='user', db=db)
    else:
        assert bind_model_account('destination', 'codex/test', db=db) == second['id']


def test_explicit_pin_keeps_its_distinct_account_move_reason(accounts, monkeypatch):
    from connections.account_selection import rebind_idle_account

    db, first, second, _events = _configured(accounts, monkeypatch, enabled=False)
    moved = rebind_idle_account('destination', second['id'], expected_runtime_epoch=0, user_id='user', db=db)
    assert moved['changed'] is True
    switch = db.execute("SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()
    assert json.loads(switch['metadata'])['reason'] == 'account_pinned'


def test_gate_after_rollover_plan_rolls_back_the_move_and_receipt(accounts, monkeypatch):
    db, first, second, events = _configured(accounts, monkeypatch)
    _accept(db, 'input')
    held = object()
    monkeypatch.setattr('infra.thread_inputs.connection_limit_gate', lambda *_a, **_k: (True, held))

    assert _start(db, 'held') is held
    assert chosen_account_id('destination', 'codex', db=db) == first['id']
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == 0
    assert events == []
