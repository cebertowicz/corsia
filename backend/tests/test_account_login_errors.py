"""Lost-login outcomes follow the login captured by the failed Turn."""
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


@pytest.mark.asyncio
async def test_added_claude_failure_signs_out_only_its_account_before_returning(claude, monkeypatch):
    """The exact expired-login error is the answer, so no status read can keep the account signed in."""
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
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=db) is True
    assert get_account(claude.other, db=db)['state'] == 'signed_out'
    assert get_account(claude.first, db=db) == before_default
    assert get_connection('claude-code', db=db) == before_source
    vendor_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_newer_login_after_the_turn_stays_signed_in(claude):
    """A sign-in made after the failed Turn started is not the login that expired."""
    from connections.account_evidence import invalidate_discovery
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    db = claude.db
    turn_on_other(claude, login=1)
    invalidate_discovery(get_account(claude.other, db=db), login_seq=2, auth_state='known', db=db)
    db.commit()
    newer = get_account(claude.other, db=db)
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=db) is True
    assert get_account(claude.other, db=db) == newer


@pytest.mark.asyncio
async def test_default_failure_is_left_to_the_provider_recheck(claude):
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    before = get_account(claude.first, db=claude.db)
    assert await claude_turn_signed_out('claude-turn', EXPIRED, db=claude.db) is False
    assert get_account(claude.first, db=claude.db) == before


@pytest.mark.asyncio
async def test_other_errors_sign_nothing_out(claude):
    from connections.vendor_accounts import get_account
    from subagents.turn_account_error import claude_turn_signed_out

    turn_on_other(claude)
    assert await claude_turn_signed_out('claude-turn', 'Claude Code exited with code 1.', db=claude.db) is False
    assert get_account(claude.other, db=claude.db)['state'] == 'signed_in'
