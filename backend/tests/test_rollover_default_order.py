"""Rollover over every permitted account when no order is saved, and for a thread outside the saved order."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from connections import account_moves, account_rollover, harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import chosen_account_id, select_account
from connections.account_receipts import capture_receipt
from connections.provider_policy import effective_policy
from llm.model_status import harness_base_row
from connections.account_selection import bind_model_account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vendor_account_fixtures import _accept, _start, accounts, receipt_of, set_legacy_policy  # noqa: F401

PROJECT = 'destination-project'


def _ready_all(db, *rows):
    for row in rows:
        receipt = capture_receipt('codex', account_id=row['id'], db=db)
        mark_discovery(row['id'], receipt=receipt, status='ready',
                       models={'codex/test': {'available': True, 'resolved_identity': 'synthetic'}}, db=db)
    db.commit()


def _limit(db, account):
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', account['id'], db=db), reason='usage_limit',
                                 resets_at='2099-01-01T00:00:00+00:00', source='rate_limit_update', db=db)


def _allow(db, account):
    harness_usage.record_allowed(provider='codex', receipt=receipt_of('codex', account['id'], db=db), source='discovery', db=db)


def _unsaved_order(db, *, enabled=True):
    """A project whose switch is on with no order ever saved."""
    set_legacy_policy(PROJECT, 'codex', allowlist=None, default_account_id=None, switch_on_limit=enabled, db=db)


def _destination(db, source):
    return account_rollover.rollover_destination('codex', source['id'], harness_base_row('codex/test', db=db), PROJECT, db=db)


@pytest.fixture
def events(monkeypatch):
    sent = []
    monkeypatch.setattr(account_moves, '_publish', sent.append)
    return sent


def test_unsaved_order_is_every_permitted_account_default_first(accounts):
    db, legacy, second, third = accounts
    _unsaved_order(db)
    policy = effective_policy(PROJECT, 'codex', db=db)
    assert account_rollover.rollover_order('codex', PROJECT, policy, db=db) == [legacy['id'], second['id'], third['id']]
    assert account_rollover.rollover_order('codex', PROJECT, {**policy, 'rollover': False}, db=db) is None


def test_limited_account_rolls_over_when_no_order_was_saved(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=second['id'], db=db)
    _unsaved_order(db)
    _limit(db, second)
    _allow(db, third)
    _accept(db, 'first')

    assert _start(db, 'turn') is not None

    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='turn'").fetchone()[0] == third['id']
    assert [item['to_account_id'] for item in events] == [third['id']]


def test_unsaved_order_with_the_switch_off_stays_inert(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=second['id'], db=db)
    _unsaved_order(db, enabled=False)
    _limit(db, second)
    _allow(db, third)
    _accept(db, 'first')

    assert type(_start(db, 'turn')).__name__ == 'HeldAdmission'
    assert events == []
    assert chosen_account_id('destination', 'codex', db=db) == second['id']


@pytest.mark.asyncio
async def test_host_access_reviewer_fails_without_project_rollover_opt_in(accounts, monkeypatch):
    from connections import vendor_accounts
    from host_access.reviewer_target import resolve_reviewer_target

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    set_legacy_policy(
        PROJECT,
        'codex',
        allowlist=[second['id'], third['id']],
        default_account_id=second['id'],
        switch_on_limit=False,
        db=db,
    )
    _limit(db, second)
    _allow(db, third)
    requested_accounts = []
    get_account = vendor_accounts.get_account

    def record_account(identity, *, db):
        requested_accounts.append(identity)
        return get_account(identity, db=db)

    monkeypatch.setattr(vendor_accounts, 'get_account', record_account)

    with pytest.raises(
        RuntimeError, match='host-access reviewer account unavailable: usage_limited'
    ):
        await resolve_reviewer_target('codex/test', requester_thread_id='destination')

    assert requested_accounts == [second['id']]


@pytest.mark.asyncio
async def test_reviewer_and_host_access_share_account_unavailable_reason(accounts, monkeypatch):
    from host_access.errors import HostAccessSupervisorError
    from host_access.reviewer_target import resolve_reviewer_target
    from llm.resolution import resolve_execution_target
    from reviewer.failures import CCUnavailableError
    from reviewer import reviewer_account

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    set_legacy_policy(
        PROJECT,
        'codex',
        allowlist=[second['id'], third['id']],
        default_account_id=second['id'],
        switch_on_limit=False,
        db=db,
    )
    _limit(db, second)
    _allow(db, third)
    monkeypatch.setattr(reviewer_account, '_reviewer_project_id', lambda _db: PROJECT)

    target = resolve_execution_target('codex/test', db=db)
    with pytest.raises(CCUnavailableError) as reviewer_error:
        reviewer_account.resolve_reviewer_account(target, db=db)
    with pytest.raises(HostAccessSupervisorError) as host_error:
        await resolve_reviewer_target('codex/test', requester_thread_id='destination')

    reason = reviewer_error.value.reason
    assert reason == 'usage_limited'
    assert str(host_error.value) == f'host-access reviewer account unavailable: {reason}'


@pytest.mark.asyncio
async def test_host_access_reviewer_rolls_over_when_project_opted_in(accounts):
    from host_access.reviewer_target import resolve_reviewer_target

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    set_legacy_policy(
        PROJECT,
        'codex',
        allowlist=[second['id'], third['id']],
        default_account_id=second['id'],
        switch_on_limit=True,
        db=db,
    )
    _limit(db, second)
    _allow(db, third)

    target = await resolve_reviewer_target('codex/test', requester_thread_id='destination')

    assert target.account_id == third['id']


def test_a_thread_outside_the_saved_order_moves_to_the_first_usable_account(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id'], third['id']], default_account_id=second['id'],
               switch_on_limit=True, db=db)
    _limit(db, second)
    _allow(db, third)

    assert _destination(db, legacy) == third['id']
    assert bind_model_account('destination', 'codex/test', db=db) == third['id']
    assert chosen_account_id('destination', 'codex', db=db) == third['id']


def test_a_thread_outside_the_saved_order_stays_when_the_switch_is_off(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id'], third['id']], default_account_id=second['id'],
               switch_on_limit=False, db=db)
    _allow(db, second)

    assert _destination(db, legacy) is None


def test_first_bind_outside_the_saved_order_lands_on_the_first_usable_account(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id'], third['id']], default_account_id=None,
               switch_on_limit=True, db=db)
    _limit(db, second)
    _allow(db, third)

    # No binding and no project default: the thread resolves to the workspace Default, outside the order.
    assert select_account('destination', 'codex', model='codex/test', db=db) == third['id']


def _three_in_order(db, second, third):
    from connections.vendor_accounts import add_account, set_account_state

    fourth = set_account_state(add_account('codex', 'Fourth')['id'], 'signed_in', expected_revision=0)
    (Path(fourth['source_home']) / 'auth.json').write_text(json.dumps({
        'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'},
    }))
    _ready_all(db, second, third, fourth)
    return [second, third, fourth]


def test_rollover_wraps_to_an_account_earlier_in_the_order(accounts, events):
    db, _, second, third = accounts
    one, two, three = _three_in_order(db, second, third)
    select_account('destination', 'codex', account_id=three['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[one['id'], two['id'], three['id']], default_account_id=three['id'],
               switch_on_limit=True, db=db)
    _limit(db, three)
    _allow(db, one)
    _limit(db, two)
    _accept(db, 'first')

    assert _destination(db, three) == one['id']
    assert _start(db, 'turn') is not None
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='turn'").fetchone()[0] == one['id']


def test_rollover_from_the_first_account_still_skips_an_exhausted_one(accounts, events):
    db, _, second, third = accounts
    one, two, three = _three_in_order(db, second, third)
    select_account('destination', 'codex', account_id=one['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[one['id'], two['id'], three['id']], default_account_id=one['id'],
               switch_on_limit=True, db=db)
    _limit(db, one)
    _limit(db, two)
    _allow(db, three)

    assert _destination(db, one) == three['id']


def test_a_queued_head_on_an_account_outside_the_order_moves_before_the_hold(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    _accept(db, 'first')
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id'], third['id']], default_account_id=second['id'],
               switch_on_limit=True, db=db)
    _limit(db, second)
    _allow(db, third)

    assert _start(db, 'turn') is not None

    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='turn'").fetchone()[0] == third['id']
    assert [(item['reason'], item['from_account_id']) for item in events] == [('account_not_permitted', legacy['id'])]
    assert db.execute("SELECT content FROM messages WHERE id=?", (events[0]['message_id'],)).fetchone()[0] == 'Moved to Codex · Third'


def test_a_queued_head_outside_the_order_stays_held_without_rollover_or_a_usable_account(accounts, events):
    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    _accept(db, 'first')
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id']], default_account_id=second['id'],
               switch_on_limit=False, db=db)
    assert _start(db, 'turn') is None
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id']], default_account_id=second['id'],
               switch_on_limit=True, db=db)
    _limit(db, second)
    assert _start(db, 'turn') is None
    assert events == []
    assert chosen_account_id('destination', 'codex', db=db) == legacy['id']


def test_the_route_saves_rollover_on_with_no_saved_order(accounts):
    from api.provider_policy import router
    from infra.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {'id': 'user'}
    with TestClient(app) as api:
        response = api.put(f'/api/provider-policy/codex?project_id={PROJECT}',
                           json={'expected_revision': 0, 'rollover': True, 'order': None, 'disabled': []})
        assert response.status_code == 200, response.text
        assert response.json()['rollover'] is True and response.json()['order'] is None


def _outside_the_order(db, legacy, second, third):
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    _accept(db, 'first')
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id'], third['id']], default_account_id=second['id'],
               switch_on_limit=True, db=db)
    _limit(db, second)
    _allow(db, third)


@pytest.mark.parametrize('rollover', [False, True])
def test_paused_held_prefix_without_destination_stays_queued_before_guards(accounts, rollover):
    import plans.boundary as boundary
    import plans.service as service
    from events.delivery import enqueue_event_wake
    from plans.boundary import PlanActor

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id']], default_account_id=second['id'],
                      switch_on_limit=rollover, db=db)
    _limit(db, second)
    plan = service.ensure_plan('thread', 'destination', db=db)
    actor = PlanActor(None, 'user', None, kind='user')
    boundary.mutate(plan.id, 'pause', {'expected_revision': service._plan_row(db, plan.id)['revision']},
                    actor=actor, provenance='test.rollover_order', mutation_id=f'pause-{rollover}', db=db)
    enqueue_event_wake('destination', 'webhook.github.pr_merged', 'PR merged', message_id='wake', db=db)

    assert _start(db, 'turn') is None
    row = db.execute("SELECT delivery_state,delivery_error,write_outcome,active_turn_id,seq FROM messages WHERE id='wake'").fetchone()
    assert tuple(row) == ('queued', 'account_project_denied', 'proved_no_write', None, None)
    assert db.execute("SELECT COUNT(*) FROM context_change_journal WHERE object_kind='plan' AND object_id=? AND change_kind='held'", (plan.id,)).fetchone()[0] == 0
    assert chosen_account_id('destination', 'codex', db=db) == legacy['id']


def test_paused_held_prefix_with_destination_commits_no_move(accounts, events):
    import plans.boundary as boundary
    import plans.service as service
    from events.delivery import enqueue_event_wake
    from plans.boundary import PlanActor

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id']], default_account_id=second['id'],
                      switch_on_limit=True, db=db)
    _allow(db, second)
    plan = service.ensure_plan('thread', 'destination', db=db)
    boundary.mutate(plan.id, 'pause', {'expected_revision': service._plan_row(db, plan.id)['revision']},
                    actor=PlanActor(None, 'user', None, kind='user'), provenance='test.rollover_order',
                    mutation_id='pause-positive', db=db)
    enqueue_event_wake('destination', 'webhook.github.pr_merged', 'PR merged', message_id='wake', db=db)

    assert _start(db, 'turn') is None
    assert db.execute("SELECT delivery_state,delivery_error FROM messages WHERE id='wake'").fetchone()[:] == ('cancelled', 'held_while_paused')
    assert chosen_account_id('destination', 'codex', db=db) == legacy['id']
    assert events == []


@pytest.mark.parametrize('rollover', [False, True])
def test_plan_failure_does_not_settle_a_no_destination_hold(accounts, monkeypatch, rollover):
    from plans.public import PlanError

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id']], default_account_id=second['id'],
                      switch_on_limit=rollover, db=db)
    _limit(db, second)
    _accept(db, 'input')

    def fail_snapshot(*_args, **_kwargs):
        raise PlanError('unreconciled')

    monkeypatch.setattr('plans.public.snapshot_after_admission', fail_snapshot)
    assert _start(db, 'turn') is None
    row = db.execute("SELECT delivery_state,delivery_error,write_outcome,active_turn_id,seq FROM messages WHERE id='input'").fetchone()
    assert tuple(row) == ('queued', 'account_project_denied', 'proved_no_write', None, None)
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == 0


def test_plan_failure_before_destination_move_records_no_move(accounts, events, monkeypatch):
    from plans.public import PlanError

    db, legacy, second, third = accounts
    _ready_all(db, legacy, second, third)
    select_account('destination', 'codex', account_id=legacy['id'], db=db)
    set_legacy_policy(PROJECT, 'codex', allowlist=[second['id']], default_account_id=second['id'],
                      switch_on_limit=True, db=db)
    _allow(db, second)
    _accept(db, 'input')
    monkeypatch.setattr('plans.public.snapshot_after_admission', lambda *_args, **_kwargs: (_ for _ in ()).throw(PlanError('unreconciled')))

    failed = _start(db, 'turn')
    assert type(failed).__name__ == 'FailedInputBatch'
    assert chosen_account_id('destination', 'codex', db=db) == legacy['id']
    row = db.execute("SELECT delivery_state,delivery_error,active_turn_id FROM messages WHERE id='input'").fetchone()
    assert row['delivery_state'] == 'failed' and 'unreconciled' in row['delivery_error'] and row['active_turn_id'] is None
    assert events == []
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == 0


def test_a_paused_thread_outside_the_order_commits_no_move(accounts, events, monkeypatch):
    db, legacy, second, third = accounts
    _outside_the_order(db, legacy, second, third)
    monkeypatch.setattr('plans.public.set_aside_while_paused', lambda *_a, **_k: True)

    assert _start(db, 'turn') is None

    assert events == []
    assert chosen_account_id('destination', 'codex', db=db) == legacy['id']
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == 0


def test_a_failed_prefix_outside_the_order_commits_no_move(accounts, events):
    from infra.database import write_transaction
    from infra.thread_inputs import fail_claimable_prefix

    db, legacy, second, third = accounts
    _outside_the_order(db, legacy, second, third)

    with write_transaction(db, scope='test_fail_prefix') as connection:
        failed = fail_claimable_prefix(connection, 'destination', error='usage_unprovable')

    assert failed is None
    assert events == []
    assert chosen_account_id('destination', 'codex', db=db) == legacy['id']
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='system'").fetchone()[0] == 0
