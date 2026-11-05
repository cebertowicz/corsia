"""Lost-login outcomes follow the login captured by the failed Turn."""
import asyncio
import logging
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


async def signed_out(turn_id, detail, *, db, include_account=False):
    """What the process-exit path does: check the Turn's login on the expired-login text, then sign it out unless it answered."""
    from subagents.turn_account_error import claude_login_verdict, sign_out_on_verdict

    verdict = await claude_login_verdict(turn_id, detail, db=db)
    return await sign_out_on_verdict(verdict, db=db, include_account=include_account)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['fail', 'error'])
async def test_added_claude_failure_signs_out_only_its_account_before_returning(claude, monkeypatch, result):
    """When the login check does not answer, the expired-login error signs out only the Turn's account."""
    from connections import account_controls
    from connections.models import get_connection
    from connections.vendor_accounts import get_account

    db = claude.db
    receipt = turn_on_other(claude)
    before_default = get_account(claude.first, db=db)
    before_source = get_connection('claude-code', db=db)
    vendor_status = AsyncMock(return_value=(True, {}, None))
    monkeypatch.setattr(account_controls, '_vendor_status', vendor_status)
    check = login_check(monkeypatch, result)
    assert await signed_out('claude-turn', EXPIRED, db=db) is True
    assert get_account(claude.other, db=db)['state'] == 'signed_out'
    assert get_account(claude.first, db=db) == before_default
    assert get_connection('claude-code', db=db) == before_source
    vendor_status.assert_not_awaited()
    check.assert_awaited_once_with('claude-code', receipt)


@pytest.mark.asyncio
async def test_login_that_still_answers_stays_signed_in(claude, monkeypatch):
    """The CLI can report an expired login that still works; one check that answers keeps it signed in."""
    from connections.vendor_accounts import get_account
    from infra.vendor_login import read_turn_receipt

    db = claude.db
    receipt = turn_on_other(claude)
    before = get_account(claude.other, db=db)
    check = login_check(monkeypatch, 'pass')
    assert await signed_out('claude-turn', EXPIRED, db=db, include_account=True) is False
    assert get_account(claude.other, db=db) == before
    assert before['state'] == 'signed_in'
    assert read_turn_receipt('claude-turn', db=db) == receipt
    check.assert_awaited_once_with('claude-code', receipt)


@pytest.mark.asyncio
async def test_login_check_timeout_signs_out_as_before(claude, monkeypatch):
    """A login check that does not finish in time proves nothing, so the error still signs out."""
    from connections import harness_usage_check
    from connections.vendor_accounts import get_account

    release = asyncio.Event()

    async def hangs(provider, account_id):
        await release.wait()
        return 'pass'

    monkeypatch.setattr(harness_usage_check, '_read', hangs)
    monkeypatch.setattr(harness_usage_check, '_CHECKS', {})
    monkeypatch.setattr(harness_usage_check, '_LOGIN_CHECK_SECONDS', 0.01)
    monkeypatch.setattr('infra.state.ws.notify_models_changed', lambda: None)
    turn_on_other(claude)
    assert await signed_out('claude-turn', EXPIRED, db=claude.db) is True
    assert get_account(claude.other, db=claude.db)['state'] == 'signed_out'
    release.set()
    await asyncio.gather(*harness_usage_check._CHECKS.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(('result', 'kind', 'state'), [
    ('pass', 'unknown', 'signed_in'), ('fail', 'sign_in_expired', 'signed_out'),
])
async def test_result_row_is_an_expired_login_only_when_the_login_did_not_answer(claude, monkeypatch, result, kind, state):
    """A login that answered is neither signed out nor classified as expired anywhere on its result row."""
    from connections.vendor_accounts import get_account
    from core.turn_core import TurnIdentity
    from subagents.cc_event_result import _handle_result_error, _result_metadata
    from subagents.turn_account_error import claude_login_verdict

    turn_on_other(claude)
    check = login_check(monkeypatch, result)
    # on_event stashes the verdict before it takes event_lock.
    ev = {'is_error': True, 'result': EXPIRED,
          '_corsia_login_verdict': await claude_login_verdict('claude-turn', EXPIRED, db=claude.db)}
    live = SimpleNamespace(interrupt_requested=False, model='claude-code/sonnet', apply_now_interrupt_turn=None)
    ctx = SimpleNamespace(db=claude.db, live=live)
    is_error, metadata = _result_metadata(
        ctx, ev, identity=TurnIdentity('claude-turn', 1), apply_now_interrupt_owns_result=False,
    )
    await _handle_result_error(ctx, ev, metadata, is_error=is_error)
    assert metadata['cause_kind'] == kind
    assert metadata.get('cause') == (kind if result == 'fail' else None)
    assert get_account(claude.other, db=claude.db)['state'] == state
    check.assert_awaited_once()


@pytest.mark.parametrize(('answered', 'kind'), [(True, 'process_exited'), (False, 'sign_in_expired')])
def test_process_exit_row_is_an_expired_login_only_when_the_login_did_not_answer(claude, answered, kind):
    from subagents.cc_runtime_errors import surface_exit_error

    stored = {}

    def insert(thread_id, role, text, meta, **_kwargs):
        stored.update(meta)
        return 'row'

    surface_exit_error('claude-thread', 1, EXPIRED, lambda *_args: None, claude.db, None,
                       insert_message_fn=insert, logger=logging.getLogger(__name__), login_answered=answered)
    assert stored['cause_kind'] == kind


@pytest.mark.asyncio
async def test_stop_takes_the_event_lock_while_the_login_check_runs(claude, monkeypatch):
    """The result's login check runs before event_lock, so a Stop or steer never waits on the CLI."""
    from connections import harness_usage_check
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    turn_on_other(claude)
    started, release = asyncio.Event(), asyncio.Event()

    async def parked_check(*_args, **_kwargs):
        started.set()
        await release.wait()
        return 'pass'

    monkeypatch.setattr(harness_usage_check, 'check_login', parked_check)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('claude-turn', 1))
    on_event = _make_on_event(live, 'claude-thread', lambda *_args: None, claude.db)
    pending = asyncio.create_task(on_event({'kind': 'result', 'is_error': True, 'result': EXPIRED}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not live.event_lock.locked()
        await asyncio.wait_for(live.event_lock.acquire(), 0.1)
        live.event_lock.release()
    finally:
        release.set()
        await pending


@pytest.mark.asyncio
async def test_a_successful_reply_quoting_the_error_checks_no_login(claude, monkeypatch):
    """Only an error result can be an expired login; a reply that quotes the text is not held."""
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    turn_on_other(claude)
    check = login_check(monkeypatch, 'fail')
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('claude-turn', 1))
    on_event = _make_on_event(live, 'claude-thread', lambda *_args: None, claude.db)
    await on_event({'kind': 'result', 'is_error': False, 'result': f'The CLI said: {EXPIRED}'})
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_newer_login_after_the_turn_stays_signed_in(claude, monkeypatch):
    """A sign-in made after the failed Turn started is not the login that expired."""
    from connections.account_evidence import invalidate_discovery
    from connections.vendor_accounts import get_account

    db = claude.db
    login_check(monkeypatch, 'fail')
    turn_on_other(claude, login=1)
    invalidate_discovery(get_account(claude.other, db=db), login_seq=2, auth_state='known', db=db)
    db.commit()
    newer = get_account(claude.other, db=db)
    assert await signed_out('claude-turn', EXPIRED, db=db) is True
    assert get_account(claude.other, db=db) == newer


@pytest.mark.asyncio
async def test_default_failure_is_left_to_the_provider_recheck(claude, monkeypatch):
    from connections.vendor_accounts import get_account

    check = login_check(monkeypatch, 'fail')
    before = get_account(claude.first, db=claude.db)
    assert await signed_out('claude-turn', EXPIRED, db=claude.db) is False
    assert get_account(claude.first, db=claude.db) == before
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_errors_sign_nothing_out(claude, monkeypatch):
    from connections.vendor_accounts import get_account

    check = login_check(monkeypatch, 'fail')
    turn_on_other(claude)
    assert await signed_out('claude-turn', 'Claude Code exited with code 1.', db=claude.db) is False
    assert get_account(claude.other, db=claude.db)['state'] == 'signed_in'
    check.assert_not_awaited()
