"""The picker names the account the thread's next turn runs on, busy or idle.

A model row reads the same rollover decision the claim path acts on
(``rollover_destination``), never an idle-gated copy of it: a thread bound to a
limited account shows each model ready on the usable account its next turn moves
to, and selecting the row binds that same account."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connections import account_moves, account_rollover, harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import chosen_account_id, select_account
from connections.account_receipts import capture_receipt
from connections.account_selection import bind_model_account
from connections.harness_usage import set_allow_overage
from connections.models import upsert_connection
from connections.vendor_accounts import add_account, default_account, set_account_state
from vendor_account_fixtures import signed_in_default
from llm.model_status import scoped_status_rows
from vendor_account_fixtures import _accept, _start, accounts, default_receipt, receipt_of, set_legacy_policy  # noqa: F401

PROJECT = 'destination-project'
LIMIT = dict(reason='usage_limit', resets_at='2099-01-01T00:00:00+00:00', source='rate_limit_update')
CREDITS = {'hasCredits': True, 'unlimited': False, 'balance': '12'}
RESET = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())


def _discover(db, provider, account, model):
    db.execute('UPDATE models SET context_window=200000 WHERE id=?', (model,))
    receipt = capture_receipt(provider, account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready',
                          models={model: {'available': True, 'resolved_identity': 'synthetic'}}, db=db)
    db.commit()


def _busy(db, thread, model, *, turn='running-turn'):
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES (?,?,0,?,'running','t','b')", (turn, thread, model))
    db.commit()


def _row(thread, model):
    return {item['id']: item for item in scoped_status_rows(thread_id=thread)}[model]


def _spend_past(db, provider, account):
    """The account's plan is spent but it spends past it on credits: usable."""
    set_allow_overage(provider, account['id'], True, db=db)
    harness_usage.record_rate_limits_read({
        'ordinaryUsageAllowed': False,
        'rateLimits': {'limitId': 'codex', 'planType': 'pro', 'credits': CREDITS, 'spendControlReached': False,
                       'rateLimitReachedType': 'rate_limit_reached', 'primary': {'usedPercent': 100, 'resetsAt': RESET}}},
        provider=provider, receipt=receipt_of(provider, account['id'], db=db), db=db)


def _policy(db, provider, shape, *, source, others, rollover=True):
    allowlists = {'unsaved': None, 'saved': [source['id'], *others], 'outside': list(others)}
    set_legacy_policy(PROJECT, provider, allowlist=allowlists[shape], default_account_id=None,
                      switch_on_limit=rollover, db=db)


# Reported thread 1: Codex bound to a limited added account; the Default spends past its plan.
@pytest.mark.parametrize('busy', [False, True], ids=['idle', 'busy'])
@pytest.mark.parametrize('shape', ['unsaved', 'saved', 'outside'])
def test_codex_bound_to_a_limited_account_reads_the_spend_past_default(accounts, busy, shape):  # noqa: F811
    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, 'codex', account, 'codex/test')
    select_account('destination', 'codex', account_id=second['id'], db=db)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', second['id'], db=db), **LIMIT)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', third['id'], db=db), **LIMIT)
    _spend_past(db, 'codex', legacy)
    _policy(db, 'codex', shape, source=second, others=[third['id'], legacy['id']])
    if busy:
        _busy(db, 'destination', 'codex/test')

    row = _row('destination', 'codex/test')
    selected = next(item for item in row['runs_on'] if item['selected'])
    assert (selected['status'], selected['account_id']) == ('ready', legacy['id'])


@pytest.mark.parametrize('rollover', [True, False], ids=['rollover-on', 'rollover-off'])
def test_the_picker_and_the_claim_name_the_same_account(accounts, rollover):  # noqa: F811
    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, 'codex', account, 'codex/test')
    select_account('destination', 'codex', account_id=second['id'], db=db)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', second['id'], db=db), **LIMIT)
    _spend_past(db, 'codex', legacy)
    _policy(db, 'codex', 'unsaved', source=second, others=[], rollover=rollover)
    _busy(db, 'destination', 'codex/test')
    row = _row('destination', 'codex/test')
    selected = next(item for item in row['runs_on'] if item['selected'])
    db.execute("UPDATE active_turns SET status='completed'")
    db.commit()
    _accept(db, 'first')

    claimed = _start(db, 'turn')

    if rollover:
        assert (selected['status'], selected['account_id']) == ('ready', legacy['id'])
        assert claimed is not None and chosen_account_id('destination', 'codex', db=db) == legacy['id']
    else:
        assert (selected['status'], selected['status_reason'], selected['account_id']) == ('unavailable', 'usage_limited', second['id'])
        assert type(claimed).__name__ == 'HeldAdmission'


# Reported thread 2: Claude Code with the Default spent and two usable added accounts.
@pytest.mark.parametrize('busy', [False, True], ids=['idle', 'busy'])
def test_claude_bound_to_the_spent_default_reads_the_first_usable_added_account(accounts, busy):  # noqa: F811
    db = accounts[0]
    model, thread = 'claude-code/test', 'claude-destination'
    upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/synthetic'})
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES (?,'Test',?,'claude-code','test','claude-code','t','t')", (model, model))
    db.execute("UPDATE models SET context_window=200000 WHERE id=?", (model,))
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES (?,'user',?,'destination-project','t','t')", (thread, model))
    db.commit()
    first, second = (set_account_state(add_account('claude-code', name)['id'], 'signed_in', expected_revision=0)
                     for name in ('First', 'Second'))
    default = signed_in_default('claude-code', db=db)
    for account in (first, second):
        _discover(db, 'claude-code', account, model)
        harness_usage.record_allowed(provider='claude-code', source='discovery', db=db,
                                     receipt=capture_receipt('claude-code', account_id=account['id'], db=db))
    harness_usage.record_limited(provider='claude-code', receipt=default_receipt('claude-code', db=db), **LIMIT)
    select_account(thread, 'claude-code', account_id=default['id'], db=db)
    set_legacy_policy(PROJECT, 'claude-code', allowlist=None, default_account_id=None,
                      switch_on_limit=True, db=db)
    if busy:
        _busy(db, thread, model)

    row = _row(thread, model)
    selected = next(item for item in row['runs_on'] if item['selected'])
    assert (selected['status'], selected['account_id']) == ('ready', first['id'])


def test_selecting_the_row_binds_the_account_it_named_once_the_thread_is_idle(accounts):  # noqa: F811
    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, 'codex', account, 'codex/test')
    select_account('destination', 'codex', account_id=second['id'], db=db)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', second['id'], db=db), **LIMIT)
    _spend_past(db, 'codex', legacy)
    _policy(db, 'codex', 'unsaved', source=second, others=[])
    _busy(db, 'destination', 'codex/test')
    row = _row('destination', 'codex/test')
    named = next(item for item in row['runs_on'] if item['selected'])['account_id']

    # A running turn keeps its account; the binding moves with the first idle selection or claim.
    assert bind_model_account('destination', 'codex/test', db=db) == second['id']
    db.execute("UPDATE active_turns SET status='completed'")
    db.commit()
    assert bind_model_account('destination', 'codex/test', db=db) == named == legacy['id']


def test_the_claim_still_owns_the_move_no_picker_read_writes_a_binding(accounts, monkeypatch):  # noqa: F811
    db, legacy, second, third = accounts
    for account in (legacy, second, third):
        _discover(db, 'codex', account, 'codex/test')
    select_account('destination', 'codex', account_id=second['id'], db=db)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', second['id'], db=db), **LIMIT)
    _spend_past(db, 'codex', legacy)
    _policy(db, 'codex', 'unsaved', source=second, others=[])
    monkeypatch.setattr(account_moves, 'commit_move', lambda *a, **k: pytest.fail('a read moved a thread'))

    row = _row('destination', 'codex/test')
    assert next(item for item in row['runs_on'] if item['selected'])['account_id'] == legacy['id']
    assert chosen_account_id('destination', 'codex', db=db) == second['id']
