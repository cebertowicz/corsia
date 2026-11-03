"""Lost-login outcomes follow the login captured by the failed Turn."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts, claude  # noqa: F401

EXPIRED = 'Not logged in · Please run /login'


def turn_on_other(claude, login=1):
    """Run ``claude-turn`` on the added account at a known login, stamped as a claim does."""
    from connections.account_evidence import invalidate_discovery
    from connections.vendor_accounts import get_account
    from infra.vendor_login import stamp_initial_claim

    db = claude.db
    invalidate_discovery(get_account(claude.other, db=db), login_seq=login, auth_state='known', db=db)
    db.execute("UPDATE active_turns SET vendor_account_id=? WHERE id='claude-turn'", (claude.other,))
    db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,seq,active_turn_id) "
               "SELECT 'claude-input',thread_id,'user','input','t',0,id FROM active_turns WHERE id='claude-turn'")
    receipt = stamp_initial_claim(db, 'claude-turn', 'claude-input')
    db.commit()
    return receipt


def login_check(monkeypatch, result):
    """Replace the account's login check with one that answers ``result``."""
    from connections import harness_usage_check

    check = AsyncMock(return_value=result)
    monkeypatch.setattr(harness_usage_check, 'check_login', check)
    return check


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['fail', 'error'])
async def test_added_claude_failure_signs_out_only_its_account_before_returning(claude, monkeypatch, result):
    """When the login check does not answer, the expired-login error signs out only the Turn's account."""
    from connections import account_controls
    from connections.models import get_connection
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    db = claude.db
    turn_on_other(claude)
    before_default = get_account(claude.first, db=db)
    before_source = get_connection('claude-code', db=db)
    vendor_status = AsyncMock(return_value=(True, {}, None))
    monkeypatch.setattr(account_controls, '_vendor_status', vendor_status)
    check = login_check(monkeypatch, result)
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=db) is True
    assert get_account(claude.other, db=db)['state'] == 'signed_out'
    assert get_account(claude.first, db=db) == before_default
    assert get_connection('claude-code', db=db) == before_source
    vendor_status.assert_not_awaited()
    check.assert_awaited_once_with('claude-code', claude.other)


@pytest.mark.asyncio
async def test_login_that_still_answers_stays_signed_in(claude, monkeypatch):
    """The CLI can report an expired login that still works; one check that answers keeps it signed in."""
    from connections.vendor_accounts import get_account
    from infra.vendor_login import read_turn_receipt
    from subagents.turn_account_error import claude_turn_signed_out

    db = claude.db
    receipt = turn_on_other(claude)
    before = get_account(claude.other, db=db)
    check = login_check(monkeypatch, 'pass')
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=db, include_account=True) is None
    assert get_account(claude.other, db=db) == before
    assert before['state'] == 'signed_in'
    assert read_turn_receipt('claude-turn', db=db) == receipt
    check.assert_awaited_once_with('claude-code', claude.other)


@pytest.mark.asyncio
async def test_login_check_timeout_signs_out_as_before(claude, monkeypatch):
    """A login check that does not finish in time proves nothing, so the error still signs out."""
    from connections import harness_usage_check
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    async def hangs(provider, account_id):
        await asyncio.sleep(5)
        return 'pass'

    monkeypatch.setattr(harness_usage_check, '_read', hangs)
    monkeypatch.setattr(harness_usage_check, '_LOGIN_CHECK_SECONDS', 0.01)
    turn_on_other(claude)
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=claude.db) is True
    assert get_account(claude.other, db=claude.db)['state'] == 'signed_out'


@pytest.mark.asyncio
@pytest.mark.parametrize(('result', 'cause'), [('pass', None), ('fail', 'sign_in_expired')])
async def test_result_marker_drops_the_expired_cause_only_when_the_login_answered(claude, monkeypatch, result, cause):
    """A login that answered must not show the sign-in-expired line on its result row."""
    from subagents.cc_event_result import _handle_result_error

    turn_on_other(claude)
    check = login_check(monkeypatch, result)
    metadata = {'turn_id': 'claude-turn', 'cause': 'sign_in_expired', 'cause_kind': 'sign_in_expired'}
    await _handle_result_error(SimpleNamespace(db=claude.db), {'result': EXPIRED}, metadata, is_error=True)
    assert metadata.get('cause') == cause
    check.assert_awaited_once()


@pytest.mark.asyncio
async def test_newer_login_after_the_turn_stays_signed_in(claude, monkeypatch):
    """A sign-in made after the failed Turn started is not the login that expired."""
    from connections.account_evidence import invalidate_discovery
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    db = claude.db
    login_check(monkeypatch, 'fail')
    turn_on_other(claude, login=1)
    invalidate_discovery(get_account(claude.other, db=db), login_seq=2, auth_state='known', db=db)
    db.commit()
    newer = get_account(claude.other, db=db)
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=db) is True
    assert get_account(claude.other, db=db) == newer


@pytest.mark.asyncio
async def test_default_failure_is_left_to_the_provider_recheck(claude, monkeypatch):
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    check = login_check(monkeypatch, 'fail')
    before = get_account(claude.first, db=claude.db)
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=claude.db) is False
    assert get_account(claude.first, db=claude.db) == before
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_errors_sign_nothing_out(claude, monkeypatch):
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    check = login_check(monkeypatch, 'fail')
    turn_on_other(claude)
    assert await claude_turn_signed_out('claude-turn', 'Claude Code exited with code 1.', db=claude.db) is False
    assert get_account(claude.other, db=claude.db)['state'] == 'signed_in'
    check.assert_not_awaited()
