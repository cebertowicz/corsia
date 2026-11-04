"""A transient Claude login failure leaves at most one invisible continue."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity, TurnTerminalResult
from infra.database import get_db
from infra.thread_driver import finish_turn
from infra.thread_input_types import ClaimedBatch
from subagents.cc_event_terminal_error import terminal_error
from subagents.cc_process_exit import finish_process_exit
from subagents.cc_runtime_state import _Live
from test_harness_overflow_rerun import _fail_rerun_write, _now, _turn

ERROR = 'Failed to authenticate. OAuth session expired and could not be refreshed'
CONTENT = ('Your previous turn ended when Claude Code briefly reported its login as failed; '
           'the login still works. Continue the work that turn was doing.')
PREFIX = 'login-race-continue:'
CAUSE = 'claude_login_race'


def _error(answered=True, outcome='error'):
    event = {'result': ERROR, '_corsia_login_verdict': ('receipt', answered)}
    return terminal_error(event, {}, outcome)


def _finish(db, turn='t1', error=None, outcome='error'):
    return finish_turn(turn, outcome=outcome, ended_at=_now(), successor_turn_id=f'next-{turn}',
                       boot_epoch='boot', error=error, db=db)


def _continues(db):
    return db.execute("SELECT id,role,content,metadata FROM messages WHERE id LIKE ?", (PREFIX + '%',)).fetchall()


def _error_notices(db, thread='thread'):
    """Visible failure rows: the result marker, the process-exit notice or the held-back notice."""
    return [tuple(row) for row in db.execute(
        "SELECT id,json_extract(metadata,'$.type'),json_extract(metadata,'$.cause_kind') FROM messages "
        "WHERE thread_id=? AND role!='user' AND id NOT LIKE ? AND (json_extract(metadata,'$.is_error') "
        "OR json_extract(metadata,'$.type') IN ('cc_error','llm_error'))", (thread, PREFIX + '%'))]


def test_answered_result_queues_exactly_one_system_continue(isolated_db):
    db = get_db()
    _turn(db)
    terminal = _finish(db, error=_error())
    rows = _continues(db)
    assert isinstance(terminal.successor, ClaimedBatch)
    assert len(rows) == 1
    row = rows[0]
    assert (row['id'], row['role'], row['content']) == (PREFIX + 't1', 'system', CONTENT)
    metadata = json.loads(row['metadata'])
    assert metadata['cause'] == metadata['event_type'] == CAUSE
    assert metadata.get('summary') is None
    assert [tuple(row) for row in db.execute("SELECT id,content FROM messages WHERE role='user'")] == [
        ('m1-thread', 'work'),
    ]


def test_unanswered_login_queues_nothing(isolated_db):
    db = get_db()
    _turn(db)
    assert isinstance(_error(), BaseException)
    _finish(db, error=_error(False))
    assert not _continues(db)


def test_other_sign_in_error_is_not_a_login_race(isolated_db):
    db = get_db()
    _turn(db)
    event = {'result': 'Not logged in · Please run /login', '_corsia_login_verdict': ('receipt', True)}
    _finish(db, error=terminal_error(event, {}, 'error'))
    assert not _continues(db)


@pytest.mark.asyncio
async def test_result_event_checks_login_and_queues_continue(isolated_db, monkeypatch):
    from subagents.cc_runtime_events import _make_on_event

    db = get_db()
    _turn(db)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('t1', 1), turn_active=True)
    monkeypatch.setattr('subagents.turn_account_error._turn_login', lambda *_args, **_kwargs: 'receipt')
    check = AsyncMock(return_value='pass')
    monkeypatch.setattr('connections.harness_usage_check.check_login', check)
    on_event = _make_on_event(live, 'thread', lambda *_: None, db)
    await on_event({'kind': 'result', 'is_error': True, 'result': ERROR})
    check.assert_awaited_once()
    assert [row['id'] for row in _continues(db)] == [PREFIX + 't1']
    assert _error_notices(db) == []  # the re-run answers; no failure notice for this Turn


@pytest.mark.asyncio
async def test_result_event_after_stop_request_does_not_rerun(isolated_db, monkeypatch):
    from subagents.cc_runtime_events import _make_on_event

    db = get_db()
    _turn(db)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('t1', 1), turn_active=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def check(*_args, **_kwargs):
        started.set()
        await release.wait()
        return 'pass'

    monkeypatch.setattr('subagents.turn_account_error._turn_login', lambda *_args, **_kwargs: 'receipt')
    monkeypatch.setattr('connections.harness_usage_check.check_login', check)
    on_event = _make_on_event(live, 'thread', lambda *_: None, db)
    pending = asyncio.create_task(on_event({'kind': 'result', 'is_error': True, 'result': ERROR}))
    try:
        await asyncio.wait_for(started.wait(), 2)
        live.checkpoint_wait_scope.request_stop()
    finally:
        release.set()
        await pending
    assert not _continues(db)


def test_failed_rerun_has_no_third_run(isolated_db):
    db = get_db()
    _turn(db)
    rerun = _finish(db, error=_error()).successor
    assert isinstance(rerun, ClaimedBatch)
    terminal = _finish(db, rerun.turn_id, error=_error())
    assert not isinstance(terminal.successor, ClaimedBatch)
    assert [row['id'] for row in _continues(db)] == [PREFIX + 't1']


def test_rerun_refused_before_it_runs_writes_the_held_back_notice(isolated_db):
    db = get_db()
    _turn(db)
    rerun = _finish(db, error=_error()).successor
    assert _error_notices(db) == []
    assert PREFIX + 't1' in _fail_rerun_write(db, rerun)
    assert _error_notices(db) == [('overflow-unanswered:t1', 'cc_error', 'unknown')]


def test_login_race_rerun_refused_for_size_is_not_rerun_again(isolated_db):
    from core.stop_causes import ContextOverflowEnd

    db = get_db()
    _turn(db)
    rerun = _finish(db, error=_error()).successor
    terminal = _finish(db, rerun.turn_id, error=ContextOverflowEnd('Prompt is too long'))
    assert not isinstance(terminal.successor, ClaimedBatch)
    assert not db.execute("SELECT 1 FROM messages WHERE id LIKE 'overflow-continue:%'").fetchone()


def test_cancelled_turn_queues_nothing(isolated_db):
    db = get_db()
    _turn(db)
    assert isinstance(_error(), BaseException)
    _finish(db, outcome='cancelled', error=_error())
    assert not _continues(db)


def test_interrupted_result_does_not_queue_rerun(isolated_db):
    db = get_db()
    _turn(db)
    event = {'result': ERROR, '_corsia_login_verdict': ('receipt', True)}
    _finish(db, error=terminal_error(event, {'stop_reason': 'interrupted'}, 'error'))
    assert not _continues(db)


@pytest.mark.asyncio
async def test_natural_exit_checks_once_and_queues_continue(isolated_db, monkeypatch):
    db = get_db()
    _turn(db)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('t1', 1), turn_active=True)
    monkeypatch.setattr('subagents.turn_account_error._turn_login', lambda *_args, **_kwargs: 'receipt')
    check = AsyncMock(return_value='pass')
    monkeypatch.setattr('connections.harness_usage_check.check_login', check)
    from subagents.cc_process_exit import handle_process_exit
    from subagents.cc_runtime_state import _LIVE

    _LIVE.put('thread', live)
    try:
        result = await handle_process_exit(
            'thread', live, 1, ERROR, lambda *_: None, db,
            finalize_callback=lambda thread, *_args, **_kwargs: _LIVE.pop(thread, None),
        )
        assert result
        check.assert_awaited_once()
        assert [row['id'] for row in _continues(db)] == [PREFIX + 't1']
        assert _error_notices(db) == []  # the re-run answers; no failure notice for this Turn
    finally:
        _LIVE.pop('thread', None)


@pytest.mark.asyncio
async def test_real_stop_during_natural_exit_check_does_not_rerun(isolated_db, monkeypatch):
    from subagents import cc_stop
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    _turn(db)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('t1', 1), turn_active=True)
    _LIVE.put('thread', live)
    started, release = asyncio.Event(), asyncio.Event()
    checks = 0

    async def check(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 1:
            started.set()
            await release.wait()
        return ('receipt', True)

    monkeypatch.setattr('subagents.cc_process_exit.claude_login_verdict', check)
    monkeypatch.setattr(cc_stop, 'halt_exact_live_before_transition', AsyncMock(return_value=True))
    monkeypatch.setattr(cc_stop, 'drive_turn_settlement', AsyncMock())
    pending = asyncio.create_task(finish_process_exit('thread', live, 1, ERROR, lambda *_: None, db))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(cc_stop.stop('thread', expected_turn_id='t1', broadcast=lambda *_: None), 3)
        assert live.checkpoint_wait_scope.stop_requested
        row = db.execute("SELECT status,error FROM active_turns WHERE id='t1'").fetchone()
        assert (row['status'], row['error']) == ('error', ERROR)
        assert not _continues(db)
    finally:
        release.set()
        result = await pending
        _LIVE.pop('thread', None)
    assert result is TurnTerminalResult.CONFLICTING_TERMINAL


@pytest.mark.asyncio
async def test_stop_wins_during_process_exit_login_check(isolated_db, monkeypatch):
    db = get_db()
    _turn(db)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=TurnIdentity('t1', 1), turn_active=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def check(*_args, **_kwargs):
        started.set()
        await release.wait()
        return ('receipt', True)

    monkeypatch.setattr('subagents.cc_process_exit.claude_login_verdict', check)
    pending = asyncio.create_task(finish_process_exit('thread', live, 1, ERROR, lambda *_: None, db))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not live.event_lock.locked()
        _finish(db, outcome='cancelled')
    finally:
        release.set()
    assert await pending is TurnTerminalResult.CONFLICTING_TERMINAL
    assert not _continues(db)
