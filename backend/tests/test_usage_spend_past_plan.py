"""'Spend past usage limit': a plan-exhaustion vendor verdict does not make an account unusable
while the switch is on and paid capacity remains; the stored vendor facts stay as reported."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connections import claude_usage, harness_usage
from connections.harness_usage import set_allow_overage
from vendor_account_fixtures import accounts, receipt_of  # noqa: F401

CREDITS = {'hasCredits': True, 'unlimited': False, 'balance': '12'}
NO_CREDITS = {'hasCredits': False, 'unlimited': False, 'balance': '0'}
RESET = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())


def _read(*, credits=CREDITS, spend_reached=False, reached='rate_limit_reached', ordinary=False):
    return {
        'ordinaryUsageAllowed': ordinary,
        'rateLimits': {
            'limitId': 'codex', 'planType': 'pro', 'credits': credits, 'spendControlReached': spend_reached,
            'rateLimitReachedType': reached, 'primary': {'usedPercent': 100, 'resetsAt': RESET},
        },
    }


def _codex(accounts, *, allow, **read):
    db, legacy, *_ = accounts
    receipt = receipt_of('codex', legacy['id'], db=db)
    if allow is not None:
        set_allow_overage('codex', legacy['id'], allow, db=db)
    harness_usage.record_rate_limits_read(_read(**read), provider='codex', receipt=receipt, db=db)
    return db, legacy['id']


def _projection(db, account_id, model='codex/test'):
    return harness_usage.project_for_model(harness_usage.stored_usage(provider='codex', account_id=account_id, db=db),
                                          model=model, provider='codex')


def test_spending_on_credits_is_usable_with_the_switch_on(accounts):
    db, account = _codex(accounts, allow=True)
    projection = _projection(db, account)
    assert projection['usable'] is True and projection['state'] == 'allowed'
    assert harness_usage.active_limit(provider='codex', account_id=account, db=db) is None
    # The vendor's own verdict is kept as reported.
    stored = harness_usage.stored_usage(provider='codex', account_id=account, db=db)
    assert stored['vendor_verdicts']['null']['state'] == 'limited'
    assert stored['vendor_verdicts']['null']['reason'] == 'rate_limit_reached'


def test_unlimited_credits_count_as_paid_capacity(accounts):
    db, account = _codex(accounts, allow=True, credits={'hasCredits': False, 'unlimited': True, 'balance': None})
    assert _projection(db, account)['usable'] is True


def test_no_credits_stays_limited_with_the_switch_on(accounts):
    db, account = _codex(accounts, allow=True, credits=NO_CREDITS)
    projection = _projection(db, account)
    assert projection['usable'] is False and projection['state'] == 'limited'


def test_spend_control_reached_stays_limited_with_the_switch_on(accounts):
    db, account = _codex(accounts, allow=True, spend_reached=True)
    assert _projection(db, account)['usable'] is False


@pytest.mark.parametrize('reached', ['workspace_owner_credits_depleted', 'workspace_member_credits_depleted'])
def test_depleted_credits_stay_limited_with_the_switch_on(accounts, reached):
    db, account = _codex(accounts, allow=True, reached=reached)
    assert _projection(db, account)['usable'] is False


def test_credits_do_not_lift_the_limit_with_the_switch_off(accounts):
    db, account = _codex(accounts, allow=None)
    projection = _projection(db, account)
    assert projection['usable'] is False and projection['reason'] == 'rate_limit_reached'


def test_switch_off_after_on_blocks_again(accounts):
    db, account = _codex(accounts, allow=True)
    assert _projection(db, account)['usable'] is True
    set_allow_overage('codex', account, False, db=db)
    assert _projection(db, account)['usable'] is False


def test_a_turns_own_usage_rejection_is_not_overridden(accounts):
    db, account = _codex(accounts, allow=True)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', account, db=db),
                                 reason='usageLimitExceeded', resets_at=None, source='turn_error', db=db)
    assert _projection(db, account)['usable'] is False


def _claude(accounts, *, allow, extra):
    db = accounts[0]
    account = _claude_default(db)
    receipt = receipt_of('claude-code', account, db=db)
    if allow:
        set_allow_overage('claude-code', account, True, db=db)
    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    claude_usage.apply_usage_response({
        'rate_limits_available': True,
        'rate_limits': {'five_hour': {'utilization': 100, 'resets_at': reset}, 'extra_usage': extra},
    }, receipt=receipt, db=db)
    return db, account


def _claude_default(db):
    from connections.models import upsert_connection
    from connections.vendor_accounts import default_account

    upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/synthetic'})
    return default_account('claude-code')['id']


def _claude_projection(db, account):
    return harness_usage.project_for_model(
        harness_usage.stored_usage(provider='claude-code', account_id=account, db=db),
        model='claude-code/test', provider='claude-code')


@pytest.mark.parametrize('extra,usable', [
    ({'is_enabled': True, 'utilization': 10}, True),
    ({'is_enabled': True, 'utilization': 100}, False),
    ({'is_enabled': False}, False),
])
def test_claude_extra_usage_decides_with_the_switch_on(accounts, extra, usable):
    db, account = _claude(accounts, allow=True, extra=extra)
    assert _claude_projection(db, account)['usable'] is usable


def test_claude_extra_usage_does_not_lift_the_limit_with_the_switch_off(accounts):
    db, account = _claude(accounts, allow=False, extra={'is_enabled': True, 'utilization': 10})
    projection = _claude_projection(db, account)
    assert projection['usable'] is False and projection['reason'] == 'plan_limit_overage_off'
