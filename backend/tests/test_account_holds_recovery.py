"""Account holds and account notices that survive a restart, a stop or a competing writer.

Pins: retained holds and their recovery, notification and boot recovery, claim readiness, plan
readiness/notices/unread state/copy/snapshot/model recovery and the wake reader contract."""
from __future__ import annotations

import asyncio
import json
import sqlite3

from fastapi import HTTPException
from loguru import logger
import pytest
import pytest_asyncio

from tests.child_parent_notice_helpers import delegated, wakes
from tests.test_fresh_threads import fresh_db
from vendor_account_fixtures import (  # noqa: F401
    _accept,
    _held,
    _kind,
    _known_default,
    _ready,
    _row,
    _start,
    accounts,
    claude,
    CompetingRead,
    held_worker,
    notification_owner,
    queue,
    ready,
)


def seed_destination(db, *, same_vendor=False, available=True):
    model = 'codex/alternate' if same_vendor else 'openai/alternate'
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,"
               "available,enabled,context_window,created_at,updated_at) VALUES (?,?,?,?,?,?,?,1,100000,'t','t')",
               (model, 'Alternate', model, 'codex' if same_vendor else 'openai', 'alternate',
                'codex' if same_vendor else 'native', int(available)))
    db.commit()
    return model


def mark(db):
    from infra.database import write_transaction
    from infra.queued_input_settlement import refresh_account_input_holds
    with write_transaction(db, scope='test_account_notice'):
        refresh_account_input_holds(db, 'worker')


def read_status(db, plan, owner, node_id=None):
    from plans import service, status
    from plans.boundary import PlanActor
    actor = PlanActor(owner, 'user', 'destination-project')
    return (status.node_detail(db, service._plan_row(db, plan.id), node_id, actor=actor)
            if node_id else status.read(db, service._plan_row(db, plan.id), actor=actor))


async def _drain_notification():
    from infra import unavailable_model_recovery as recovery

    previous = recovery._TASK
    # Submit and notify use the real app-loop handoff, each a scheduled callback.
    for _ in range(12):
        await asyncio.sleep(0)
        if recovery._TASK is not None and recovery._TASK is not previous:
            break
    assert recovery._TASK is not None and recovery._TASK is not previous
    await asyncio.wait_for(asyncio.shield(recovery._TASK), timeout=5)


def _refresh(db):
    from connections.account_holds import refresh_thread_holds
    from infra.database import write_transaction

    with write_transaction(db, scope='test_notification_refresh'):
        refresh_thread_holds('destination', db=db)


def _choose(db, account):
    from connections.account_policy import select_account

    select_account('destination', 'codex', account_id=account['id'], db=db)


# The held-input instructions stay true whether zero or one other permitted account is left, so
# they are fixed text per cause (r15 test (f)): no longer "cancel the held input first".
REMOVED_ACTION = 'Choose another permitted account if one is available; otherwise switch model.'
DENIED_ACTION = ('Restore the permitted project policy through an authorized control, or ask the user to restore it; '
                 'otherwise the user can choose another permitted account if one is available, or switch model.')


@pytest.mark.parametrize('cause,phrase,action,agent_action', [
    ('account_signed_out', 'it is signed out', 'Sign in to the permitted account through the account controls.', 'The user can sign in to the permitted account through the account controls.'),
    ('account_not_configured', 'no account is signed in for this vendor', 'Sign one in under Settings > Providers.', 'The user can sign one in under Settings > Providers.'),
    ('account_removed', 'the account was removed', REMOVED_ACTION, 'The user can choose another permitted account if one is available; otherwise switch model.'),
    ('account_project_denied', 'project policy does not permit the account', DENIED_ACTION, DENIED_ACTION),
    ('project_policy_missing', "the thread's project policy is missing", 'Restore the project or move the thread to a permitted project through an authorized control, or ask the user to do so.', 'Restore the project or move the thread to a permitted project through an authorized control, or ask the user to do so.'),
    ('account_model_not_discovered', 'the selected model has not been discovered for this account', 'Verify the signed-in account through the account controls to discover its models.', 'The user can verify the signed-in account through the account controls to discover its models.'),
    ('account_runtime_pending', "this account's runtime is not yet available", 'Inspect the held work and report the missing runtime capability; signing in again will not enable it.', 'Inspect the held work and report the missing runtime capability; signing in again will not enable it.'),
    ('account_login_unverified', "the account's current sign-in could not be verified", 'Verify the signed-in account through the account controls to obtain fresh account evidence.', 'The user can verify the signed-in account through the account controls to obtain fresh account evidence.'),
    ('codex_auth_projection_changed', 'the managed authentication link changed', 'Sign in again through the account controls so the old managed link can be cleaned up and fresh account evidence obtained.', 'The user can sign in again through the account controls so the old managed link can be cleaned up and fresh account evidence obtained.'),
    ('codex_auth_source_missing', 'the authentication source is missing', 'Sign in again through the account controls to restore the source and obtain fresh account evidence.', 'The user can sign in again through the account controls to restore the source and obtain fresh account evidence.'),
])
def test_one_map_exact_human_text_and_agent_action_scope(cause, phrase, action, agent_action):
    from prompts.plan_wakes import account_readiness_detail, account_wake_fields
    readiness = {'ready': False, 'cause': cause}
    human = account_readiness_detail(readiness)
    assert human == f'Its account is not ready: {phrase}. {action}'
    assert cause not in human
    state, fields = account_wake_fields(readiness)
    assert state == 'unready' and fields['cause'] == cause and fields['phrase'] == phrase
    assert fields['action'] == agent_action
    assert 'CLI' not in human and 'provider source' not in human


@pytest.mark.parametrize('rollover', [True, False])
@pytest.mark.parametrize('other_permitted', [0, 1])
def test_removed_and_turned_off_instructions_are_identical_for_any_other_account_and_rollover(rollover, other_permitted):
    """Test (f): the exact held-input strings, as the two producing functions return them."""
    from prompts.plan_wakes import account_readiness_detail, account_wake_fields

    removed = {'ready': False, 'cause': 'account_removed'}
    denied = {'ready': False, 'cause': 'account_project_denied'}

    assert account_readiness_detail(removed) == (
        'Its account is not ready: the account was removed. Choose another permitted account if one is available; '
        'otherwise switch model.')
    assert account_wake_fields(removed)[1]['action'] == (
        'The user can choose another permitted account if one is available; otherwise switch model.')
    assert account_readiness_detail(denied) == (
        'Its account is not ready: project policy does not permit the account. ' + DENIED_ACTION)
    assert account_wake_fields(denied)[1]['action'] == DENIED_ACTION
    assert 'cancel' not in account_wake_fields(removed)[1]['action'].lower()


@pytest.mark.parametrize('readiness', [None, {'ready': False, 'cause': 'untrusted-provider-output'}])
def test_missing_unknown_facts_conservatively_render_unread(readiness):
    from prompts.plan_wakes import ACCOUNT_READINESS_UNREAD, account_readiness_detail, account_wake_fields
    assert account_readiness_detail(readiness) == ACCOUNT_READINESS_UNREAD
    state, fields = account_wake_fields(readiness)
    assert state == 'unready' and fields['cause'] == 'account_readiness_unread'
    assert 'untrusted-provider-output' not in str(fields)


def test_pending_public_notice_keeps_history_and_fixed_chip_without_readiness_wake(held_worker):
    from infra.database import write_transaction
    from plans.public import notify_turn_error
    from tests.child_parent_notice_helpers import wakes
    from vendor_account_fixtures import ready

    db = held_worker[0]
    queue(db)
    mark(db)
    original = dict(wakes(db)[-1])
    ready(db, held_worker[2])
    before = len(wakes(db))
    mark(db)
    assert len(wakes(db)) == before
    # Independent eligible public notice, not a readiness notification.
    with write_transaction(db, scope='test_pending_account_notice'):
        notify_turn_error(db, 'worker', turn_id=None, error='account_signed_out')
    pending = wakes(db)[-1]
    assert len(wakes(db)) == before + 1
    assert json.loads(pending['metadata'])['summary'] == 'Delegated worker is waiting to resume'
    assert 'Its account is ready; the input is waiting for its claim to resume.' in pending['content']
    assert 'it is signed out' not in pending['content']
    assert 'No recovery deadline is promised.' in pending['content']
    assert dict(db.execute('SELECT * FROM messages WHERE id=?', (original['id'],)).fetchone()) == original


@pytest.mark.parametrize('kind,state', [('child_provider_refused', 'invalid'), ('child_turn_errored', 'unready')])
def test_append_rejects_invalid_account_metadata_before_duplicate_or_admission(held_worker, kind, state):
    from plans.plan_wake import append
    from infra.database import write_transaction
    db, *_, plan, node = held_worker
    before = tuple(db.iterdump())
    with pytest.raises(ValueError, match='invalid account hold notice state'):
        with write_transaction(db, scope='test_invalid_account_notice'):
            append(db, recipient='parent', kind=kind, subject_id='invalid', revision=1,
                   plan_id=plan.id, content='invalid', account_hold_state=state)
    assert tuple(db.iterdump()) == before


def test_rewording_an_action_keeps_who_can_take_it(monkeypatch):
    from prompts import plan_wakes
    from prompts.plan_wakes import account_wake_fields

    monkeypatch.setitem(plan_wakes.ACCOUNT_HOLD_COPY, 'account_removed',
                        ('the account was removed', 'Use the inbox to cancel the held input.', 'user'))
    _, removed = account_wake_fields({'ready': False, 'cause': 'account_removed'})
    assert removed['action'] == 'The user can use the inbox to cancel the held input.'


@pytest.mark.asyncio
@pytest.mark.parametrize('same_vendor', [False, True])
async def test_actual_model_route_completion_keeps_same_brief_and_conditional_claim(held_worker, monkeypatch, same_vendor):
    from api.delegation_model import set_thread_model
    from infra.thread_execution import drive_thread

    db, _, custom, _, *_ = held_worker
    model = seed_destination(db, same_vendor=same_vendor)
    queue(db)
    mark(db)
    original = db.execute("SELECT content,input_order FROM messages WHERE id='brief'").fetchone()
    claims = []

    async def paid_edge(batch, **kwargs):
        claims.append(batch)
        return batch

    monkeypatch.setattr('infra.thread_execution.execute_claimed_batch', paid_edge)
    monkeypatch.setattr('api.delegation_model.authenticate', lambda _: {'id': 'user'})
    result = await set_thread_model('worker', 'synthetic-route-auth', {
        'model': model, 'from_thread_id': 'parent', 'reason': 'authorized same-child recovery'})
    if same_vendor:
        assert result.status_code == 202
        receipt = json.loads(result.body)
        assert receipt['switched'] is False and receipt['operation_id']
        assert receipt['status'] == 'failed', receipt
    else:
        assert result['ok'] is True and result['changed'] is True and result['new_model'] == model
    await drive_thread('worker')
    row = db.execute("SELECT * FROM messages WHERE id='brief'").fetchone()
    assert (row['content'], row['input_order']) == tuple(original)
    assert row['target_model'] == ('codex/test' if same_vendor else model)
    if same_vendor:
        assert claims == [] and row['active_turn_id'] is None
        assert row['delivery_error'] == 'account_model_not_discovered'
    else:
        assert len(claims) == 1 and [i.message_id for i in claims[0].items] == ['brief']
        assert row['active_turn_id'] == claims[0].turn_id and row['delivery_error'] is None
        await drive_thread('worker')
        assert len(claims) == 1
    assert db.execute("SELECT COUNT(*) FROM messages WHERE thread_id='worker' AND content=?", (original['content'],)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_actual_route_rejects_unowned_sender_without_changing_hold(held_worker, monkeypatch):
    from api.delegation_model import set_thread_model
    db = held_worker[0]
    model = seed_destination(db)
    queue(db)
    mark(db)
    before = tuple(db.iterdump())
    monkeypatch.setattr('api.delegation_model.authenticate', lambda _: {'id': 'different-user'})
    with pytest.raises(HTTPException) as exc:
        await set_thread_model('worker', 'synthetic-route-auth', {'model': model, 'from_thread_id': 'parent'})
    assert exc.value.status_code == 403
    assert tuple(db.iterdump()) == before


@pytest.mark.asyncio
async def test_actual_route_pending_receipt_keeps_input_unclaimed_until_source_finishes(held_worker, monkeypatch):
    from api.delegation_model import set_thread_model
    from infra.input_writes import begin_batch_write
    from infra.thread_inputs import start_idle_turn
    from vendor_account_fixtures import ready

    db, _, custom, _, *_ = held_worker
    ready(db, custom)
    queue(db, 'already-owned')
    batch = start_idle_turn('worker', turn_id='running-source', boot_epoch='boot', started_at='t', db=db)
    assert batch is not None
    begin_batch_write(batch, attempt_id='ambiguous', started_at='t', db=db)
    queue(db)
    model = seed_destination(db)
    monkeypatch.setattr('api.delegation_model.authenticate', lambda _: {'id': 'user'})
    result = await set_thread_model('worker', 'synthetic-route-auth', {'model': model, 'from_thread_id': 'parent'})
    assert result.status_code == 202
    body = json.loads(result.body)
    assert body['accepted'] and body['switched'] is False and body['operation_id']
    assert 'delegate_status' in body['instruction']
    assert db.execute("SELECT active_turn_id FROM messages WHERE id='brief'").fetchone()[0] is None
    assert db.execute("SELECT model FROM threads WHERE id='worker'").fetchone()[0] == 'codex/test'


@pytest.mark.asyncio
async def test_denied_target_and_public_queued_rebind_moves_unwritten_hold_only(held_worker, monkeypatch):
    from api.delegation_model import set_thread_model
    from connections.account_policy import AccountAdmissionError
    from connections.account_selection import rebind_idle_account
    from vendor_account_fixtures import set_legacy_policy

    db, _, custom, other, *_ = held_worker
    model = seed_destination(db, same_vendor=True)
    queue(db)
    mark(db)
    moved = rebind_idle_account('worker', other['id'], expected_runtime_epoch=0, user_id='user', db=db)
    assert moved == {'account_id': other['id'], 'runtime_context_epoch': 1, 'changed': True}
    # Actor and destination both belong to this project; denial is the actual policy owner.
    set_legacy_policy('destination-project', 'codex', allowlist=[], default_account_id=None, db=db)
    before = tuple(db.iterdump())
    monkeypatch.setattr('api.delegation_model.authenticate', lambda _: {'id': 'user'})
    with pytest.raises(HTTPException) as exc:
        await set_thread_model('worker', 'synthetic-route-auth', {'model': model, 'from_thread_id': 'parent'})
    assert exc.value.status_code == 409 and 'account_project_denied' in exc.value.detail
    assert tuple(db.iterdump()) == before


def test_first_null_turn_repeat_and_meaningful_cause_share_existing_episode(held_worker):
    from connections.account_lifecycle import change_state
    from infra.database import write_transaction
    from plans.public import notify_turn_error

    db, _, custom, _, *_ = held_worker
    before = len(wakes(db))
    queue(db)
    mark(db)
    first = wakes(db)[before:]
    assert len(first) == 1
    assert json.loads(first[0]['metadata'])['summary'] == 'Delegated worker is waiting for its account'
    assert '(account_model_not_discovered)' in first[0]['content']
    assert 'The user can verify' in first[0]['content']
    assert 'as review evidence from your own assigned plan task to your parent' in first[0]['content']
    assert 'without a replacement delegate_send' in first[0]['content']
    assert 'Stop' not in first[0]['content']
    mark(db)
    with write_transaction(db, scope='test_account_notice'):
        notify_turn_error(db, 'worker', turn_id=None, error='account_model_not_discovered')
    assert len(wakes(db)) == before + 1
    signed_out = change_state(custom['id'], 'signed_out', expected_revision=custom['revision'], db=db)
    assert len(wakes(db)) == before + 2
    assert '(account_signed_out)' in wakes(db)[-1]['content']
    change_state(custom['id'], 'removed', expected_revision=signed_out['revision'], db=db)
    assert len(wakes(db)) == before + 3
    assert '(account_removed)' in wakes(db)[-1]['content']
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='worker'").fetchone()[0] == 0
    head = db.execute("SELECT * FROM messages WHERE id='brief'").fetchone()
    assert (head['active_turn_id'], head['delivery_state'], head['write_outcome']) == (None, 'queued', 'proved_no_write')


def test_notice_and_marker_rollback_together(held_worker):
    from infra.database import write_transaction
    from infra.queued_input_settlement import refresh_account_input_holds

    db = held_worker[0]
    queue(db)
    before = len(wakes(db))
    prior = tuple(db.execute("SELECT delivery_error,write_outcome FROM messages WHERE id='brief'").fetchone())
    with pytest.raises(RuntimeError, match='abort'):
        with write_transaction(db, scope='test_account_notice'):
            refresh_account_input_holds(db, 'worker')
            assert len(wakes(db)) == before + 1
            raise RuntimeError('abort')
    assert len(wakes(db)) == before
    assert tuple(db.execute("SELECT delivery_error,write_outcome FROM messages WHERE id='brief'").fetchone()) == prior
    mark(db)
    assert len(wakes(db)) == before + 1


@pytest.mark.parametrize('negative', ['trailing', 'ordinary', 'wrong_target', 'model', 'running', 'waiting_retry'])
def test_noneligible_heads_do_not_emit_account_notice(held_worker, negative):
    from infra.database import write_transaction
    from infra.queued_input_settlement import offer_account_head_notice

    db = held_worker[0]
    if negative == 'trailing':
        queue(db, 'predecessor')
        db.execute("UPDATE messages SET delivery_error='unrelated' WHERE id='predecessor'")
        db.commit()
    if negative == 'ordinary':
        from vendor_account_fixtures import ready
        ready(db, held_worker[2])
    queue(db)
    if negative == 'ordinary':
        before = len(wakes(db))
        with write_transaction(db, scope='test_account_notice'):
            offer_account_head_notice(db, 'worker')
        assert len(wakes(db)) == before
        return
    if negative == 'wrong_target':
        db.execute("UPDATE messages SET target_model='another/model' WHERE id='brief'")
    if negative == 'model':
        db.execute("UPDATE messages SET delivery_error='selected_model_unavailable',write_outcome='proved_no_write' WHERE id='brief'")
    if negative in ('running', 'waiting_retry'):
        db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,retry_at) "
                   "VALUES ('live','worker',0,'codex/test',?,'t','b','2099-01-01T00:00:00')", (negative,))
    db.commit()
    before = len(wakes(db))
    mark(db)
    assert len(wakes(db)) == before


@pytest.mark.asyncio
async def test_actual_inbox_cancel_drive_exposes_retained_head_without_account_event(held_worker):
    from api.thread_inbox_actions import run_inbox_message_action

    db = held_worker[0]
    queue(db, 'predecessor')
    db.execute("UPDATE messages SET delivery_error='unrelated' WHERE id='predecessor'")
    db.commit()
    queue(db)
    before = len(wakes(db))
    mark(db)
    assert len(wakes(db)) == before
    result = await run_inbox_message_action('worker', 'predecessor', 'user', action='cancel')
    assert result['message']['delivery_state'] == 'cancelled'
    assert len(wakes(db)) == before + 1
    assert '(account_model_not_discovered)' in wakes(db)[-1]['content']
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='worker'").fetchone()[0] == 0
    assert db.execute("SELECT content FROM messages WHERE id='brief'").fetchone()[0] == 'Keep this exact brief'
    mark(db)
    assert len(wakes(db)) == before + 1


def test_retained_head_after_stale_wake_settlement_is_reoffered(held_worker):
    from infra.database import write_transaction
    from infra.queued_input_settlement import still_claimable

    db = held_worker[0]
    queue(db, 'stale')
    db.execute("UPDATE messages SET continuation_condition=? WHERE id='stale'",
               (json.dumps({'kind': 'plan_wake', 'subject_id': 'stale', 'transition': 'child_returned:1',
                            'details': {'plan_id': 'missing-plan'}}),))
    db.commit()
    queue(db)
    # The earlier queued row hides the retained head from the shared predicate.
    db.execute("UPDATE messages SET delivery_error='unrelated' WHERE id='stale'")
    db.commit()
    before = len(wakes(db))
    mark(db)
    assert len(wakes(db)) == before
    with write_transaction(db, scope='test_account_notice'):
        rows = db.execute("SELECT * FROM messages WHERE thread_id='worker' AND delivery_state='queued' ORDER BY input_order").fetchall()
        current = still_claimable(db, 'worker', rows)
    assert [r['id'] for r in current] == ['brief']
    assert len(wakes(db)) == before + 1


def test_both_real_status_consumers_show_live_pending_separate_from_history(held_worker):
    from connections.account_lifecycle import change_state
    from plans import service
    from tests.test_child_parent_notice_stalled_child import _start
    from tests.child_parent_notice_helpers import wakes

    db, _, custom, _, step, plan, node = held_worker
    _start(db, node)
    queue(db)
    mark(db)
    parent_plan = service.ensure_plan('thread', 'parent', db=db)
    own = read_status(db, plan, 'worker')
    parent = read_status(db, parent_plan, 'parent')
    assert 'Verify the signed-in account' in str(read_status(db, plan, 'worker', node.id))
    assert 'Verify the signed-in account' in str(read_status(db, parent_plan, 'parent', step.id))
    assert next(r for r in own['current']['rows'] if r['id'] == node.id)['label'] == 'waiting'
    db.commit()  # a state change publishes after its own commit, so it must own the transaction
    custom = change_state(custom['id'], 'signed_out', expected_revision=custom['revision'], db=db)
    notice = wakes(db)[-1]
    assert 'it is signed out' in notice['content']
    custom = change_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
    ready(db, custom)
    count = len(wakes(db))
    before = tuple(db.iterdump())
    for result in (read_status(db, plan, 'worker', node.id), read_status(db, parent_plan, 'parent', step.id)):
        assert 'Its account is ready; the input is waiting for its claim to resume.' in str(result)
        assert 'it is signed out' not in str(result)
    assert tuple(db.iterdump()) == before
    assert len(wakes(db)) == count
    assert db.execute('SELECT content FROM messages WHERE id=?', (notice['id'],)).fetchone()[0] == notice['content']


def test_advisory_current_binding_y_not_retained_marker_x(held_worker):
    from connections import read_account_readiness
    db, _, custom, other, *_ = held_worker
    queue(db)
    mark(db)
    ready(db, other)
    # Defensive stale binding injection, never the publicly refused queued rebind.
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='worker'", (other['id'],))
    db.commit()
    assert read_account_readiness(['worker'], db=db) == {
        'worker': {'ready': True, 'cause': None}}
    assert db.execute("SELECT delivery_error FROM messages WHERE id='brief'").fetchone()[0] == 'account_model_not_discovered'


@pytest.mark.parametrize('state', ['signed_out', 'removed', 'undiscovered', 'ready', 'unverified', 'c6', 'stale'])
def test_loaded_evaluator_matches_actual_require_claim(accounts, state):
    from connections import read_account_readiness
    from connections.account_policy import AccountAdmissionError, require_claim, select_account
    from connections.vendor_accounts import get_account, set_account_state
    from connections.account_evidence import discovery_record, store_record
    from infra.database import write_transaction

    db, _, custom, _ = accounts
    select_account('destination', 'codex', account_id=custom['id'], db=db)
    if state in ('signed_out', 'removed'):
        set_account_state(custom['id'], state, expected_revision=custom['revision'], db=db)
    if state == 'ready':
        ready(db, custom)
    if state in ('unverified', 'c6', 'stale'):
        with write_transaction(db, scope='test_account_readiness'):
            record = discovery_record(get_account(custom['id'], db=db))
            if state == 'unverified':
                record['auth_state'] = 'unknown'
            elif state == 'c6':
                record['auth_projection']['failed'] = {
                    'cause': 'codex_auth_projection_changed', 'account_revision': custom['revision'],
                    'login_seq': record['login_seq']}
            else:
                record['auth_projection']['stale'] = [{'phase': 'pending'}]
            store_record(custom['id'], record, db=db)
    try:
        require_claim('codex', thread_id='destination', model='codex/test', db=db)
        cause = None
    except AccountAdmissionError as exc:
        cause = exc.reason
    assert read_account_readiness(['destination'], db=db) == {
        'destination': {'ready': cause is None, 'cause': cause}}



def test_readiness_deduplicates_missing_targets_without_effects_or_private_reads(accounts):
    from connections import read_account_readiness

    db, default, *_ = accounts
    _known_default(db, default)
    db.executemany("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES (?,'user','codex/test','t','t')",
                   [(f'batch-{index}',) for index in range(901)])
    db.commit()
    ids = [f'batch-{index}' for index in range(901)]
    before = tuple(db.iterdump())
    queries = []
    db.set_trace_callback(queries.append)
    try:
        assert read_account_readiness([], db=db) == {}
        assert queries == []
        result = read_account_readiness([*ids, *ids, 'missing'], db=db)
    finally:
        db.set_trace_callback(None)

    # Canonical selection is bounded per resolved target, not by an obsolete SQL shape.
    assert len(queries) <= 10 * len(ids) + 2
    assert all(query.startswith(('SELECT ', 'WITH ')) for query in queries)
    assert all('SELECT *' not in query and 'usage_json' not in query for query in queries)
    assert all('identity_json' not in query or '$.default_comparison.auth_state' in query for query in queries)
    assert len(result) == 901
    assert all(row == {'ready': True, 'cause': None} for row in result.values())
    assert tuple(db.iterdump()) == before


def test_readiness_without_a_configured_account_matches_claim(accounts):
    from connections import read_account_readiness
    from connections.account_policy import AccountAdmissionError, require_claim

    db = accounts[0]
    db.execute("UPDATE vendor_accounts SET state='removed' WHERE connection_id='test-codex'")
    db.execute("DELETE FROM thread_vendor_accounts WHERE thread_id='destination' AND provider='codex'")
    db.commit()
    with pytest.raises(AccountAdmissionError) as exc:
        require_claim('codex', thread_id='destination', model='codex/test', db=db)
    assert exc.value.reason == 'account_not_configured'
    assert read_account_readiness(['destination'], db=db)['destination'] == {
        'ready': False, 'cause': 'account_not_configured'}


def test_null_ambient_allowlist_denial_and_missing_project(accounts):
    from connections import read_account_readiness
    from connections.account_policy import AccountAdmissionError, require_claim
    db, default, *_ = accounts
    # A project deny-list disables the remaining configured workspace account.
    db.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES ('destination-project',1)")
    codex_units = [row['id'] for row in db.execute("SELECT a.id FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id WHERE c.provider='codex'")]
    db.execute("INSERT INTO provider_policies(project_id,provider_key,rollover,disabled_json,revision) "
               "VALUES ('destination-project','codex',0,?,1)", (json.dumps(codex_units),))
    db.commit()
    assert read_account_readiness(['destination'], db=db)['destination'] == {
        'ready': False, 'cause': 'account_project_denied'}
    with pytest.raises(AccountAdmissionError) as exc:
        require_claim('codex', thread_id='destination', model='codex/test', db=db)
    assert exc.value.reason == 'account_project_denied'
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM projects WHERE id='destination-project'")
    db.commit()
    assert read_account_readiness(['destination'], db=db)['destination']['cause'] == 'project_policy_missing'


def test_exact_target_wins_and_ambiguous_native_missing_are_omitted(accounts):
    from connections import read_account_readiness
    db, default, *_ = accounts
    _known_default(db, default)
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('alias','Alias','codex/test','native','alias','native','t','t')")
    db.commit()
    assert read_account_readiness(['destination'], db=db)['destination']['ready']
    db.execute("UPDATE threads SET model='unknown' WHERE id='destination'")
    db.commit()
    assert read_account_readiness(['destination'], db=db) == {}
    db.execute("UPDATE threads SET model='alias' WHERE id='destination'")
    db.commit()
    assert read_account_readiness(['destination'], db=db) == {}
    db.execute("UPDATE models SET litellm_model='shared' WHERE id IN ('alias','codex/test')")
    db.execute("UPDATE threads SET model='shared' WHERE id='destination'")
    db.commit()
    assert read_account_readiness(['destination'], db=db) == {}


@pytest.mark.parametrize('consumer', ['own', 'parent'])
def test_each_consumer_owns_consistent_kind_and_readiness_snapshot(held_worker, consumer):
    from plans import operator_stopped, service, wait_causes
    from tests.test_child_parent_notice_stalled_child import _start

    db, _, custom, _, step, plan, node = held_worker
    _start(db, node)
    db.commit()
    queue(db)
    mark(db)
    ready(db, custom)
    assert not db.in_transaction
    path = db.execute('PRAGMA database_list').fetchone()[2]
    competitor = sqlite3.connect(path)
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    observed = CompetingRead(db, competitor, custom['id'])
    try:
        if consumer == 'own':
            nodes = [(service._node_row(db, node.id), service._plan_row(db, plan.id))]
            facts = operator_stopped.nodes(nodes, {node.id: 'worker'}, {node.id: {'selection': {'attempt_id': 'a'}}}, db=observed)
            assert facts['worker']['stalled'] == 'account'
            assert facts['worker']['account_readiness'] == {'ready': True, 'cause': None}
        else:
            parent_plan = service.ensure_plan('thread', 'parent', db=db)
            db.commit()
            nodes = [(service._node_row(db, step.id), service._plan_row(db, parent_plan.id))]
            facts = wait_causes.delegation_causes(nodes, db=observed)
            assert 'Its account is ready; the input is waiting for its claim to resume.' in facts[step.id][0]['reason']
        assert observed.fired
        assert not db.in_transaction
        from connections import read_account_readiness
        assert read_account_readiness(['worker'], db=db)['worker']['cause'] == 'account_signed_out'
    finally:
        competitor.close()


def test_unread_catalog_account_head_notifies_once_with_fixed_fallback(held_worker):
    from connections import read_account_readiness
    from plans.wait_causes import stalled_why
    from prompts.plan_wakes import ACCOUNT_HELD_CHIP

    db = held_worker[0]
    queue(db)
    # The real admission has retained this exact unwritten row for its account.
    prior = tuple(db.execute("SELECT content,input_order,active_turn_id,delivery_state,"
                             "write_outcome,delivery_error FROM messages WHERE id='brief'").fetchone())
    assert prior[2:] == (None, 'queued', 'proved_no_write', 'account_model_not_discovered')
    # Actual stored catalog loss, not a patched readiness result or admission owner: the model is in
    # neither the stored rows nor any account's discovery (the catalog is projected from the accounts').
    db.execute("DELETE FROM models WHERE id='codex/test'")
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import list_accounts

    for account in list_accounts(provider='codex', db=db):
        receipt = capture_receipt('codex', account_id=account['id'], db=db)
        if receipt is not None:
            assert mark_discovery(account['id'], receipt=receipt, status='ready', models={}, db=db)
    db.commit()
    assert stalled_why(db, 'worker') == 'account'
    assert read_account_readiness(['worker'], db=db) == {}
    before = len(wakes(db))
    mark(db)
    appended = wakes(db)[before:]
    assert len(appended) == 1
    assert '(account_readiness_unread)' in appended[0]['content']
    assert 'its readiness could not be read' in appended[0]['content']
    assert json.loads(appended[0]['metadata'])['summary'] == ACCOUNT_HELD_CHIP
    mark(db)
    assert len(wakes(db)) == before + 1
    after = tuple(db.execute("SELECT content,input_order,active_turn_id,delivery_state,"
                             "write_outcome,delivery_error FROM messages WHERE id='brief'").fetchone())
    assert after == prior
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='worker'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE thread_id='worker' AND input_order IS NOT NULL").fetchone()[0] == 1


def test_emitted_account_wake_metadata_and_copy(held_worker):
    from plans.public import notify_turn_error
    from tests.child_parent_notice_helpers import wakes
    from vendor_account_fixtures import ready
    from infra.database import write_transaction

    db = held_worker[0]
    queue(db)
    mark(db)
    held = wakes(db)[-1]
    ready(db, held_worker[2])
    with write_transaction(db, scope='test_pending_resume'):
        notify_turn_error(db, 'worker', turn_id=None, error='account_signed_out')
    pending = wakes(db)[-1]
    for row, state, chip in [
        (held, 'unready', 'Delegated worker is waiting for its account'),
        (pending, 'pending_resume', 'Delegated worker is waiting to resume'),
    ]:
        metadata = json.loads(row['metadata'])
        assert metadata['event_type'] == 'plan.child_provider_refused'
        assert metadata['account_hold_state'] == state
        assert metadata['summary'] == chip
    assert 'it is signed out' not in pending['content']


def test_partial_fixture_compiles_all_stalled_reader_branches(fresh_db):
    from plans.public import stalled_why

    db = fresh_db
    db.execute("INSERT INTO threads(id,user_id,model) VALUES ('reader','u','claude-code/opus')")
    assert stalled_why(db, 'reader') is None
    db.execute("INSERT INTO active_turns(thread_id,status,started_at) VALUES ('reader','error','t')")
    assert stalled_why(db, 'reader') == 'error'
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,input_order,target_model,"
               "delivery_state,delivery_error,write_outcome) VALUES "
               "('head','reader','user','x','t',1,'claude-code/opus','queued','account_signed_out','proved_no_write')")
    assert stalled_why(db, 'reader') == 'account'
    db.execute("UPDATE messages SET delivery_error='selected_model_unavailable'")
    assert stalled_why(db, 'reader') == 'model'
    for status in ('running', 'waiting_retry'):
        db.execute('UPDATE active_turns SET status=?', (status,))
        assert stalled_why(db, 'reader') is None


def test_cost_event_observes_one_durable_row_after_writer_release(isolated_db, monkeypatch):
    from core import turns_util
    from infra.database import get_db

    seen = []

    def emit(category, action, **props):
        db = get_db()
        try:
            assert not db.in_transaction
            assert db.execute("SELECT COUNT(*) FROM llm_usage WHERE thread_id='commit-order'").fetchone()[0] == 1
            seen.append((category, action))
        finally:
            db.close()

    monkeypatch.setattr(turns_util, 'emit_product_event', emit)
    turns_util._log_llm_usage('commit-order', 'claude-haiku-4-5', {
        'prompt_tokens': 100, 'completion_tokens': 50, 'cache_creation_tokens': 0,
        'cache_read_tokens': 80, 'total_tokens': 150, 'cost_usd': 0.000123,
    }, ttft_ms=240)
    assert seen == [('cost', 'recorded')]


@pytest.mark.asyncio
@pytest.mark.parametrize('cause', ['codex_auth_projection_changed', 'account_model_not_discovered'])
async def test_actual_boot_claims_ready_account_head_once_after_lost_notification(accounts, monkeypatch, cause):
    from automation.restart_startup import reconcile_startup_continuity, drive_startup_continuity
    from infra.database import close_thread_db, get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from connections.account_receipts import capture_receipt

    db, _, custom, _ = accounts
    _choose(db, custom)
    if cause == 'codex_auth_projection_changed':
        from pathlib import Path
        from connections.account_projection import ensure_projection, validate_projection
        from connections.account_policy import AccountAdmissionError

        old = _ready(db, custom)
        Path(custom['source_home'], 'auth.json').write_text('synthetic noncredential')
        home = Path(custom['runtime_home'])
        ensure_projection(old, home=home, kind='live', db=db)
        link = home / 'auth.json'
        link.unlink()
        link.write_text('synthetic replacement')
        with pytest.raises(AccountAdmissionError, match=cause):
            validate_projection(old, home=home, db=db)
    _accept(db, 'm')
    _held(db, cause=cause)
    before = _row(db)
    close_thread_db()
    db = get_db()
    down, wakes, _rolled_back = await reconcile_startup_continuity(logger)
    assert down.batches == down.settlements == () and wakes == ()
    assert _row(db) == before
    assert db.execute('SELECT COUNT(*) FROM active_turns').fetchone()[0] == 0
    if cause == 'codex_auth_projection_changed':
        from connections.vendor_accounts import set_account_state
        from connections.account_projection import cleanup_stale

        custom = set_account_state(custom['id'], 'signed_in', expected_revision=custom['revision'], db=db)
        fresh = capture_receipt('codex', account_id=custom['id'], db=db)
        assert cleanup_stale(fresh, quiesced=True, db=db)
    _ready(db, custom)  # Stored readiness: application loop is absent, notification is lost.
    receipt = capture_receipt('codex', account_id=custom['id'], db=db)
    assert _row(db) == before
    close_thread_db()
    db = get_db()
    ready, wakes, _rolled_back = await reconcile_startup_continuity(logger)
    assert ready.settlements == () and wakes == () and len(ready.batches) == 1
    batch = ready.batches[0]
    assert batch.vendor_receipt == receipt
    assert [item.message_id for item in batch.items] == ['m']
    assert batch.items[0].revision == before['delivery_revision'] + 1
    assert _row(db)['delivery_error'] is None
    writes = []

    async def synthetic_paid_edge(claimed, **kwargs):
        begin_batch_write(claimed, attempt_id='boot-write', started_at='write')
        settle_batch_write('boot-write', outcome='accepted')
        writes.append(claimed)
        return claimed

    monkeypatch.setattr('infra.thread_execution.execute_claimed_batch', synthetic_paid_edge)
    await drive_startup_continuity(ready, wakes, logger)
    assert writes == [batch]
    after = _row(db)
    assert (after['delivery_state'], after['write_outcome']) == ('delivered', 'accepted')
    repeated, wakes, _rolled_back = await reconcile_startup_continuity(logger)
    assert repeated.batches == repeated.settlements == () and wakes == ()
    await drive_startup_continuity(repeated, wakes, logger)
    assert writes == [batch] and _row(db) == after
    assert db.execute('SELECT COUNT(*) FROM active_turns').fetchone()[0] == 1


def test_claim_needs_own_exact_model_not_default_or_another_account(accounts):
    from connections.account_policy import AccountAdmissionError, require_claim, select_account

    db, default, custom, other = accounts
    select_account('destination', 'codex', account_id=custom['id'], db=db)
    _ready(db, other)
    with pytest.raises(AccountAdmissionError) as refused:
        require_claim('codex', thread_id='destination', model='codex/test', db=db)
    assert refused.value.reason == 'account_model_not_discovered'
    _ready(db, custom, model='codex/different')
    with pytest.raises(AccountAdmissionError) as refused:
        require_claim('codex', thread_id='destination', model='codex/test', db=db)
    assert refused.value.reason == 'account_model_not_discovered'
    _ready(db, custom)
    assert require_claim('codex', thread_id='destination', model='codex/test', db=db) == custom['id']


def test_missing_concrete_launch_does_not_claim_a_ready_custom_transport(accounts):
    from connections.account_policy import AccountAdmissionError, require_claim, require_launch, select_account

    db, _, custom, _ = accounts
    select_account('destination', 'codex', account_id=custom['id'], db=db)
    _ready(db, custom)
    assert require_claim('codex', thread_id='destination', model='codex/test', db=db) == custom['id']
    with pytest.raises(AccountAdmissionError) as refused:
        require_launch('codex', thread_id='destination', model='codex/test', db=db)
    assert refused.value.reason == 'account_runtime_pending'


def test_signed_out_default_refuses_claim_without_changing_lifecycle(accounts):
    from connections.account_policy import AccountAdmissionError, require_claim
    from connections.vendor_accounts import get_account

    from connections.vendor_accounts import set_account_state

    db, default, _, _ = accounts
    set_account_state(default['id'], 'signed_out', expected_revision=default['revision'], db=db)
    with pytest.raises(AccountAdmissionError) as refused:
        require_claim('codex', thread_id='destination', model='codex/test', account_id=default['id'], db=db)
    assert refused.value.reason == 'account_signed_out'
    assert get_account(default['id'], db=db)['state'] == 'signed_out'


def test_current_durable_link_failure_gates_every_claim_before_home_preparation(accounts):
    import json
    from connections.account_policy import AccountAdmissionError, require_claim, select_account
    from connections.vendor_accounts import get_account

    db, _, custom, _ = accounts
    select_account('destination', 'codex', account_id=custom['id'], db=db)
    receipt = _ready(db, custom)
    record = get_account(custom['id'], db=db)['discovery']
    record['auth_projection']['failed'] = {'cause': 'codex_auth_projection_changed',
        'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq, 'at': 't'}
    db.execute('UPDATE vendor_accounts SET discovery_json=? WHERE id=?', (json.dumps(record), custom['id']))
    db.commit()
    with pytest.raises(AccountAdmissionError) as refused:
        require_claim('codex', thread_id='destination', model='codex/test', db=db)
    assert refused.value.reason == 'codex_auth_projection_changed'


@pytest.mark.asyncio
async def test_sessionless_restart_link_change_head_resumes_once_from_real_notification(accounts, notification_owner):
    from infra.database import close_thread_db, get_db
    from infra.state.ws import notify_models_changed

    db, _, custom, _ = accounts
    _choose(db, custom)
    _accept(db, 'm')
    _held(db, cause='codex_auth_projection_changed')
    _ready(db, custom)
    before = _row(db)
    close_thread_db()  # Durable provenance, no retained runtime session or DB handle.
    db = get_db()
    _refresh(db)
    assert _row(db) == before
    assert _kind(db) == ('account', 'account')
    await _drain_notification()
    assert len(notification_owner) == 1
    batch = notification_owner[0]
    assert batch.vendor_receipt.account_id == custom['id']
    assert [item.message_id for item in batch.items] == ['m']
    after = _row(db)
    assert (after['delivery_state'], after['delivery_error'], after['write_outcome']) == ('delivered', None, 'accepted')
    assert batch.items[0].revision == before['delivery_revision'] + 1  # Claim clears the marker.
    assert after['delivery_revision'] == batch.items[0].revision + 1  # Accepted write settles.
    assert _kind(db) == (None, None)
    notify_models_changed()
    await _drain_notification()
    assert len(notification_owner) == 1
    assert _row(db) == after


@pytest.mark.asyncio
async def test_unbound_denied_default_recovers_without_a_deleted_hold_row(accounts, notification_owner):
    from vendor_account_fixtures import set_legacy_policy

    db, default, _, _ = accounts
    _known_default(db, default)
    set_legacy_policy('destination-project', 'codex', allowlist=[], default_account_id=None, db=db)
    _accept(db, 'm')
    assert _row(db)['delivery_error'] == 'account_project_denied'
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts WHERE thread_id='destination'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_account_holds WHERE thread_id='destination'").fetchone()[0] == 0
    set_legacy_policy('destination-project', 'codex', allowlist=[default['id']], default_account_id=default['id'], db=db)
    assert _kind(db) == ('account', 'account')
    await _drain_notification()
    assert len(notification_owner) == 1
    assert notification_owner[0].vendor_receipt.account_id == default['id']
    assert _row(db)['delivery_state'] == 'delivered'


@pytest.mark.asyncio
async def test_raising_driver_retains_marker_until_repeated_ready_refresh(accounts, notification_owner, monkeypatch):
    from infra import thread_execution

    db, _, custom, _ = accounts
    _choose(db, custom)
    _accept(db, 'm')
    _held(db)
    _ready(db, custom)
    original = thread_execution.drive_idle_thread
    attempts = []

    async def driver(thread_id, **kwargs):
        attempts.append(thread_id)
        if len(attempts) == 1:
            raise RuntimeError('synthetic first driver failure')
        return await original(thread_id, **kwargs)

    monkeypatch.setattr(thread_execution, 'drive_idle_thread', driver)
    before = _row(db)
    _refresh(db)
    await _drain_notification()
    assert attempts == ['destination']
    assert _row(db) == before
    assert notification_owner == []
    assert db.execute('SELECT COUNT(*) FROM thread_vendor_account_holds').fetchone()[0] == 0
    _refresh(db)  # Still ready, no hold row exists to delete a second time.
    await _drain_notification()
    assert attempts == ['destination', 'destination']
    assert len(notification_owner) == 1
    assert _row(db)['delivery_state'] == 'delivered'


@pytest.mark.asyncio
async def test_one_unready_attempt_per_notification_and_read_selector_has_no_writes(accounts, notification_owner, monkeypatch):
    from infra import thread_execution, unavailable_model_recovery as recovery
    from infra.state.ws import notify_models_changed

    db, _, custom, _ = accounts
    _choose(db, custom)
    _accept(db, 'm')
    before = _row(db)
    statements = []
    db.set_trace_callback(statements.append)
    assert recovery._held_thread_ids() == ['destination']
    db.set_trace_callback(None)
    assert len(statements) == 1 and statements[0].lstrip().startswith('SELECT')
    original = thread_execution.drive_idle_thread
    attempts = []

    async def count_driver(thread_id, **kwargs):
        attempts.append(thread_id)
        return await original(thread_id, **kwargs)

    monkeypatch.setattr(thread_execution, 'drive_idle_thread', count_driver)
    for count in (1, 2):
        notify_models_changed()
        await _drain_notification()
        assert attempts == ['destination'] * count
        assert _row(db) == before
        assert notification_owner == []
        assert db.execute('SELECT COUNT(*) FROM active_turns').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_delegate_send_names_account_model_discovery_hold(accounts):
    """An agent receipt distinguishes an account-held input from ordinary queued work."""
    from connections.account_policy import select_account
    from tools.delegate_send import _send

    db, _, custom, _ = accounts
    select_account('destination', 'codex', account_id=custom['id'], db=db)
    target = dict(db.execute("SELECT * FROM threads WHERE id='destination'").fetchone())

    result = await _send(
        'destination', target, 'continue the task', 'actor',
        {'user_id': 'user', 'title': 'Sender'}, {},
    )

    assert result['status'] == 'queued'
    assert result['held'] == 'Held: account_model_not_discovered.'


@pytest.mark.asyncio
async def test_two_actual_drivers_contend_for_one_claim_and_never_duplicate_delivery(accounts, notification_owner):
    from infra.thread_execution import drive_idle_thread

    db, _, custom, _ = accounts
    _choose(db, custom)
    _accept(db, 'm')
    _held(db)
    _ready(db, custom)
    await asyncio.gather(drive_idle_thread('destination'), drive_idle_thread('destination'))
    assert len(notification_owner) == 1
    assert db.execute('SELECT COUNT(*) FROM active_turns').fetchone()[0] == 1
    assert _row(db)['delivery_state'] == 'delivered'


def test_ready_refresh_retains_marker_and_revision_until_idle_claim(accounts):
    from connections.account_holds import refresh_thread_holds
    from infra.database import write_transaction

    db, _, custom, _ = accounts
    _choose(db, custom)
    _accept(db, 'm')
    assert _start(db, 'unready') is None
    before = _row(db)
    assert before['delivery_error'] == 'account_model_not_discovered'
    _ready(db, custom)
    with write_transaction(db, scope='test_account_recovery'):
        refresh_thread_holds('destination', wake=False, db=db)
    assert _row(db) == before
    assert _kind(db) == ('account', 'account')
    batch = _start(db, 'ready')
    assert batch is not None
    after = _row(db)
    assert (after['delivery_error'], after['write_outcome']) == (None, None)
    assert after['delivery_revision'] == before['delivery_revision'] + 1
    assert after['active_turn_id'] == 'ready'
    assert _kind(db) == (None, None)


@pytest.mark.parametrize('cause,kind', [('account_signed_out', 'account'), ('codex_auth_projection_changed', 'account'), ('selected_model_unavailable', 'model'), ('other', None)])
def test_shared_kind_uses_actual_head_and_current_saved_selector(accounts, cause, kind):
    db, _, _, _ = accounts
    _accept(db, 'm')
    _held(db, cause=cause)
    assert _kind(db) == (kind, kind)
    db.execute("UPDATE messages SET target_model='codex/other' WHERE id='m'")
    assert _kind(db) == (None, None)


def test_earlier_plain_or_unproved_head_never_selects_later_hold(accounts):
    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, 'first')
    _accept(db, 'later')
    _held(db, 'later')
    assert _kind(db) == (None, None)
    _held(db, 'first')
    assert _kind(db) == ('account', 'account')
    db.execute("UPDATE messages SET write_outcome=NULL WHERE id='first'")
    assert _kind(db) == (None, None)


def test_same_account_safe_boundary_clears_only_through_last_send(accounts):
    from infra.thread_inputs import claim_safe_boundary
    from infra.input_admission import accept_thread_input

    db, _, custom, _ = accounts
    _choose(db, custom)
    _ready(db, custom)
    _accept(db, 'initial')
    _start(db, 'T')
    _accept(db, 'm')
    accept_thread_input('destination', role='system', content='later', intent='queue_after_turn', message_id='later', db=db)
    _held(db, 'm')
    _held(db, 'later')
    bindings = list(map(tuple, db.execute('SELECT * FROM thread_vendor_accounts')))
    holds = list(map(tuple, db.execute('SELECT * FROM thread_vendor_account_holds')))
    later = _row(db, 'later')
    before = _row(db)
    batch = claim_safe_boundary('destination', 'T', db=db)
    assert [item.message_id for item in batch.items] == ['m']
    assert _row(db)['delivery_revision'] == before['delivery_revision'] + 1
    assert _row(db)['delivery_error'] is None
    assert _row(db, 'later') == later
    assert list(map(tuple, db.execute('SELECT * FROM thread_vendor_accounts'))) == bindings
    assert list(map(tuple, db.execute('SELECT * FROM thread_vendor_account_holds'))) == holds


@pytest.mark.parametrize('case', ['unready', 'other-account', 'model', 'stop'])
def test_boundary_refusal_preserves_fifo_and_metadata(accounts, case):
    from infra.thread_inputs import claim_safe_boundary

    db, _, custom, other = accounts
    _choose(db, custom)
    _ready(db, custom)
    _accept(db, 'initial')
    _start(db, 'T')
    _accept(db, 'm')
    _held(db, cause='selected_model_unavailable' if case == 'model' else 'account_model_not_discovered')
    _accept(db, 'plain')
    if case == 'unready':
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (custom['id'],))
    elif case == 'other-account':
        _ready(db, other)
        db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='destination'", (other['id'],))
    elif case == 'stop':
        db.execute("UPDATE active_turns SET status='cancelled' WHERE id='T'")
    db.commit()
    before = [_row(db, identity) for identity in ('m', 'plain')]
    assert claim_safe_boundary('destination', 'T', db=db) is None
    assert [_row(db, identity) for identity in ('m', 'plain')] == before
    if case == 'other-account':
        db.execute("UPDATE active_turns SET status='completed' WHERE id='T'")
        db.commit()
        batch = _start(db, 'successor')
        assert batch.vendor_receipt.account_id == other['id']
        assert [item.message_id for item in batch.items] == ['m', 'plain']


def test_repeated_ready_refresh_wakes_after_commit_and_rollback_does_not(accounts, monkeypatch):
    from connections.account_holds import refresh_thread_holds
    from infra.database import write_transaction

    db, _, custom, _ = accounts
    _choose(db, custom)
    _accept(db, 'm')
    _held(db)
    _ready(db, custom)
    seen = []
    monkeypatch.setattr('infra.state.ws.schedule_models_changed', lambda: seen.append('wake'))
    for _ in range(2):
        with write_transaction(db, scope='test_account_recovery'):
            refresh_thread_holds('destination', db=db)
            assert len(seen) == _
        assert len(seen) == _ + 1
    with pytest.raises(RuntimeError, match='rollback'):
        with write_transaction(db, scope='test_account_recovery'):
            refresh_thread_holds('destination', db=db)
            raise RuntimeError('rollback')
    assert seen == ['wake', 'wake']
    assert _kind(db) == ('account', 'account')
