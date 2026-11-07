"""The human action reads only its current chosen limited account, in one snapshot."""
from __future__ import annotations

import re
from dataclasses import replace

import pytest

from human_usage_fixtures import accounts, held_subject  # noqa: F401


class ReadTrace:
    def __init__(self, open_db):
        self.open_db = open_db
        self.queries = []

    def open(self):
        db = self.open_db()
        db.set_trace_callback(self.queries.append)
        return db


@pytest.mark.asyncio
async def test_receipt_read_budget_has_no_history_or_account_sweep(held_subject, monkeypatch):
    from infra import held_usage_recheck
    from infra.db_async import run_in_db
    from infra.held_admission import limited_admission

    db, provider, model, account, receipt = held_subject
    held = limited_admission('destination', model, db=db)
    trace = ReadTrace(held_usage_recheck.get_db)
    monkeypatch.setattr(held_usage_recheck, 'get_db', trace.open)
    assert await run_in_db(held_usage_recheck._limited_receipt, held) == (provider, receipt)
    selects = [query for query in trace.queries if query.lstrip().upper().startswith('SELECT')]
    assert selects
    assert not any('messages' in query.lower() for query in selects)
    assert all('where' in query.lower() for query in selects)
    account_reads = [query for query in selects if 'from vendor_accounts' in query.lower()]
    assert account_reads
    assert all(re.search(r'\b(?:a\.)?id\s*=', query.lower().partition(' where ')[2])
               and account['id'] in query for query in account_reads), account_reads
    assert trace.queries[0] == 'BEGIN'
    print(f'{provider}: chosen limited receipt reads={len(selects)}; history reads=0; one snapshot')


@pytest.mark.asyncio
async def test_historical_hold_cannot_borrow_a_different_accounts_allowance(held_subject):
    from connections import harness_usage
    from connections.account_evidence import mark_discovery
    from connections.account_selection import rebind_idle_account
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, set_account_state
    from infra.db_async import run_in_db
    from infra.held_admission import limited_admission
    from infra.held_usage_recheck import _limited_receipt
    from vendor_account_fixtures import set_legacy_policy

    db, provider, model, old, _ = held_subject
    held = limited_admission('destination', model, db=db)
    other = add_account(provider, 'Current chosen account', db=db)
    other = set_account_state(other['id'], 'signed_in', expected_revision=other['revision'], db=db)
    receipt = capture_receipt(provider, account_id=other['id'], db=db)
    mark_discovery(other['id'], receipt=receipt, status='ready', models={model: {'available': True}}, db=db)
    db.commit()
    harness_usage.record_allowed(provider=provider, receipt=receipt, source='test', db=db)
    set_legacy_policy('destination-project', provider, allowlist=[old['id'], other['id']],
                      default_account_id=old['id'], switch_on_limit=False, db=db)
    await run_in_db(rebind_idle_account, 'destination', other['id'], user_id='user')
    assert await run_in_db(_limited_receipt, held) is None
    harness_usage.record_limited(provider=provider, receipt=receipt, reason='current-account-limit',
                                 resets_at=None, source='test', db=db)
    assert await run_in_db(_limited_receipt, held) == (provider, receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['unknown', 'expired', 'overage', 'other_scope', 'matching_scope'])
async def test_recheck_uses_canonical_current_model_projection(held_subject, state):
    from connections import harness_usage
    from infra.db_async import run_in_db
    from infra.held_admission import limited_admission
    from infra.held_usage_recheck import _limited_receipt

    db, provider, model, account, receipt = held_subject
    held = limited_admission('destination', model, db=db)
    harness_usage.clear_account_usage(provider, account['id'], db=db)
    if state == 'overage':
        harness_usage.record_usage(provider=provider, receipt=receipt, windows=[{
            'kind': 'codex_primary' if provider == 'codex' else 'five_hour',
            'used_percent': 100, 'resets_at': '2099-01-01T00:00:00+00:00',
        }], db=db)
        assert limited_admission('destination', model, db=db) is not None
    elif state != 'unknown':
        scope = 'test' if state == 'matching_scope' else 'other-tier' if state == 'other_scope' else None
        harness_usage.record_limited(
            provider=provider, receipt=receipt, reason='test', source='test', scope_model=scope,
            resets_at='2000-01-01T00:00:00+00:00' if state == 'expired' else None, db=db,
        )
    if state == 'overage':
        await run_in_db(harness_usage.set_allow_overage, provider, account['id'], True, db=db)
    expected = (provider, receipt) if state == 'matching_scope' else None
    assert (limited_admission('destination', model, db=db) is not None) == (expected is not None)
    assert await run_in_db(_limited_receipt, held) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('connection', ['unknown', 'native', 'external'])
async def test_nonvendor_hold_does_not_read_accounts(held_subject, monkeypatch, connection):
    from infra import held_usage_recheck
    from infra.db_async import run_in_db
    from infra.held_admission import limited_admission

    db, _, model, _, _ = held_subject
    held = replace(limited_admission('destination', model, db=db), connection=connection)
    trace = ReadTrace(held_usage_recheck.get_db)
    monkeypatch.setattr(held_usage_recheck, 'get_db', trace.open)
    assert await run_in_db(held_usage_recheck._limited_receipt, held) is None
    assert trace.queries == []
