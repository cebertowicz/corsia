"""Recovery guidance and durable notices share the existing browser owner."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from host_access import browser_sessions as sessions
from host_access.processes import ProcessCleanupError, ProcessOwner
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401
from delegation_fixtures import deleg_db  # noqa: F401


@pytest.mark.asyncio
async def test_process_shutdown_recovers_after_late_browser_ack(synthetic_browser, tmp_path):
    fake = synthetic_browser
    fake.env_changes['TEST_CLOSE_DELAY'] = '0.2'
    fake.owner.close_timeout = 0.01
    owner = ProcessOwner(runtime_dir=tmp_path, browser_owner=fake.owner)
    await fake.owner.run('thread', ['snapshot'])
    with pytest.raises(ProcessCleanupError):
        await owner.shutdown()
    runtime = fake.owner._sessions['thread']
    assert runtime.close_task is not None
    await runtime.close_task
    fake.owner.close_timeout = EXIT_TIMEOUT
    await fake.owner.close_thread('thread')
    assert owner.live_count() == 0
    await owner.shutdown()


@pytest.mark.asyncio
async def test_lease_pending_failure_is_not_retained_after_verified_close(synthetic_browser, tmp_path):
    fake = synthetic_browser
    fake.env_changes['TEST_CLOSE_DELAY'] = '0.2'
    fake.owner.close_timeout = 0.01
    owner = ProcessOwner(runtime_dir=tmp_path, browser_owner=fake.owner)
    await fake.owner.run('thread', ['snapshot'], lease_id='lease')
    with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
        await owner.terminate_lease('lease')
    assert caught.value.recovery == 'pending'
    await fake.owner._sessions['thread'].close_task  # Verified close: the producer acknowledged.
    fake.owner.close_timeout = EXIT_TIMEOUT
    assert await fake.owner.close_thread('thread') == 'Browser session closed.'
    assert owner.live_count() == 0 and not owner._failures
    await owner.shutdown()


def test_operator_guidance_gives_a_truthful_recovery_path():
    from host_access import browser_reporting
    tool = sessions.BrowserSessionCleanupError('x').tool_message
    for text in (browser_reporting.BLOCKED_NOTICE, tool):
        assert 'agent-browser process' in text and 'restart CORSIA' in text
        assert 'close' in text and 'not proof' not in text
    assert 'does not by itself prove' in browser_reporting.BLOCKED_NOTICE
    assert 'Do not kill processes yourself' in tool


@pytest.mark.asyncio
async def test_pending_close_is_typed_and_has_no_operator_notice(synthetic_browser, monkeypatch):
    from host_access import browser_reporting
    report = AsyncMock()
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    fake = synthetic_browser
    fake.env_changes['TEST_CLOSE_DELAY'] = '0.2'
    fake.owner.close_timeout = 0.01
    await fake.owner.run('thread', ['snapshot'])
    with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
        await fake.owner.close_thread('thread')
    assert caught.value.recovery == 'pending'
    report.assert_not_awaited()
    runtime = fake.owner._sessions['thread']
    assert runtime.close_task is not None
    await runtime.close_task
    fake.owner.close_timeout = EXIT_TIMEOUT
    await fake.owner.close_thread('thread')
    report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['command', 'close', 'lease', 'shutdown'])
async def test_terminal_failure_reports_once_and_stops_retry_advice(synthetic_browser, monkeypatch, route):
    from host_access import browser_reporting
    report = AsyncMock(return_value=('thread', 'notice'))
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'], lease_id='lease')
    runtime = owner._sessions['thread']
    runtime.attempt = sessions.CommandResult(-1, '', '', completion='unknown')
    failure = await owner._record_failure(runtime, sessions.BrowserSessionCleanupError('ownership unresolved'))
    assert failure.recovery == 'operator_required'
    for _ in range(2):
        with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
            if route == 'command':
                await owner.run('thread', ['snapshot'], lease_id='lease')
            elif route == 'close':
                await owner.close_thread('thread')
            elif route == 'lease':
                await owner.terminate_lease('lease')
            else:
                await owner.shutdown()
        assert caught.value.recovery == 'operator_required'
        assert 'retry browser_close' not in str(caught.value).lower()
    assert report.await_count == 1
    assert owner.live_count() == 1


@pytest.mark.asyncio
async def test_acknowledged_slow_exit_stays_pending(synthetic_browser, monkeypatch):
    from host_access import browser_reporting
    report = AsyncMock()
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    owner = synthetic_browser.owner
    synthetic_browser.env_changes['TEST_HOLD_EXIT'] = '1'
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    # The acknowledgement is awaited to completion, never raced; only the held exit is bounded.
    runtime.close_task = asyncio.create_task(owner._request_close(runtime))
    await runtime.close_task
    owner.close_timeout = 0.01
    with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
        await owner.close_thread('thread')
    task = runtime.close_task
    assert task.done() and task.exception() is None
    assert caught.value.recovery == 'pending'
    report.assert_not_awaited()
    synthetic_browser.release_exit(runtime.record.session)
    owner.close_timeout = EXIT_TIMEOUT
    await owner.close_thread('thread')
    assert owner.live_count() == 0


@pytest.mark.asyncio
async def test_cancel_after_notice_commit_preserves_receipt(synthetic_browser, deleg_db, monkeypatch):
    from host_access import browser_reporting
    seed_audience(deleg_db)
    entered, release = asyncio.Event(), asyncio.Event()
    original = browser_reporting.broadcast_message_rows

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(browser_reporting, 'broadcast_message_rows', paused)
    monkeypatch.setattr('infra.state.broadcast_to_thread', lambda *args: None)
    owner = synthetic_browser.owner
    await owner.run('hidden', ['snapshot'])
    runtime = owner._sessions['hidden']
    actual_result = runtime.attempt
    runtime.attempt = sessions.CommandResult(-1, '', '', completion='unknown')
    attempt = asyncio.create_task(owner.close_thread('hidden'))
    await entered.wait()
    assert deleg_db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    release.set()
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await owner.close_thread('hidden')
    assert deleg_db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    runtime.attempt = actual_result
    await owner.close_thread('hidden')
    assert owner.live_count() == 0
    assert deleg_db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['lease', 'shutdown'])
async def test_queued_admission_rejection_has_no_cleanup_effect(synthetic_browser, monkeypatch, gate):
    from host_access import browser_reporting
    report = AsyncMock()
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'], lease_id='lease')
    runtime = owner._sessions['thread']
    before = runtime.record
    await runtime.command_lock.acquire()
    task = asyncio.create_task(owner.run('thread', ['snapshot']))
    try:
        for _ in range(100):
            if owner.active_command_count():
                break
            await asyncio.sleep(0.01)
        assert owner.active_command_count() == 1
        if gate == 'lease':
            owner._closed_leases.add('lease')
        else:
            owner._shutting_down = True
    finally:
        runtime.command_lock.release()
    with pytest.raises(sessions.BrowserSessionError, match='admission is closed'):
        await task
    assert runtime.record == before
    report.assert_not_awaited()
    await owner.close_thread('thread')


def seed_audience(db, *, parent_user='u-test', parent_project=None):
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at,project_id) VALUES ('visible',?,'Parent','t','t',?)", (parent_user, parent_project))
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at,spawned_by,parent_thread_id) VALUES ('hidden','u-test','Worker','t','t','visible','visible')")
    db.commit()


@pytest.mark.asyncio
async def test_hidden_notice_and_recovery_are_durable_in_visible_owner(deleg_db, monkeypatch):
    from host_access import browser_reporting
    seed_audience(deleg_db)
    events = []
    monkeypatch.setattr('infra.state.broadcast_to_thread', lambda tid, payload: events.append((tid, payload)))
    notice = await browser_reporting.report_cleanup('hidden')
    assert notice[0] == 'visible'
    await browser_reporting.report_cleanup('hidden', recovered=notice)
    rows = deleg_db.execute("SELECT id,role,content,seq FROM messages WHERE thread_id='visible' ORDER BY seq").fetchall()
    assert len(rows) == len(events) == 2
    assert all(row['role'] == 'system' for row in rows)
    assert 'could not be verified' in rows[0]['content']
    assert 'verified' in rows[1]['content']
    assert all(tid == 'visible' for tid, _ in events)
    assert [event['message']['seq'] for _, event in events] == [row['seq'] for row in rows]
    assert deleg_db.execute("SELECT COUNT(*) FROM messages WHERE thread_id='hidden'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_visible_peer_stays_its_own_notice_audience(deleg_db, monkeypatch):
    from host_access import browser_reporting
    seed_audience(deleg_db)
    deleg_db.execute("UPDATE threads SET spawned_by=NULL WHERE id='hidden'")
    deleg_db.commit()
    monkeypatch.setattr('infra.state.broadcast_to_thread', lambda *args: None)
    notice = await browser_reporting.report_cleanup('hidden')
    assert notice[0] == 'hidden'


@pytest.mark.asyncio
async def test_foreign_parent_is_not_a_notice_audience(deleg_db):
    from host_access import browser_reporting
    seed_audience(deleg_db, parent_user='someone-else')
    with pytest.raises(ValueError):
        await browser_reporting.report_cleanup('hidden')
    assert deleg_db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_lineage', ['cycle', 'missing', 'project'])
async def test_invalid_lineage_never_broadcasts_elsewhere(deleg_db, bad_lineage):
    from host_access import browser_reporting
    seed_audience(deleg_db)
    if bad_lineage == 'project':
        deleg_db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('different','Different','t','t')")
        deleg_db.execute("UPDATE threads SET project_id='different' WHERE id='visible'")
    else:
        deleg_db.execute("UPDATE threads SET spawned_by=? WHERE id='visible'", ('hidden' if bad_lineage == 'cycle' else 'missing',))
    deleg_db.commit()
    with pytest.raises(ValueError):
        await browser_reporting.report_cleanup('hidden')
    assert deleg_db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_initial_notice_failure_can_retry_and_recovery_failure_cannot_poison_owner(synthetic_browser, monkeypatch, caplog):
    from host_access import browser_reporting
    report = AsyncMock(side_effect=[OSError('private-path'), ('thread', 'notice'), OSError('private-path')])
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    actual_result = runtime.attempt
    runtime.attempt = sessions.CommandResult(-1, '', '', completion='unknown')
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await owner.close_thread('thread')
    assert runtime.report is None and owner.live_count() == 1
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await owner.close_thread('thread')
    assert runtime.report == ('thread', 'notice')
    runtime.attempt = actual_result
    assert await owner.close_thread('thread') == 'Browser session closed.'
    assert owner.live_count() == 0 and report.await_count == 3
    await owner.shutdown()
    assert 'private-path' not in caplog.text
    assert 'recovery notice could not be recorded' in caplog.text


@pytest.mark.asyncio
async def test_recovery_notice_cancellation_cannot_retain_certified_owner(synthetic_browser, monkeypatch):
    from host_access import browser_reporting
    monkeypatch.setattr(browser_reporting, 'report_cleanup', AsyncMock(side_effect=asyncio.CancelledError))
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    owner._sessions['thread'].report = ('thread', 'notice')
    with pytest.raises(asyncio.CancelledError):
        await owner.close_thread('thread')
    assert owner.live_count() == 0
    await owner.shutdown()


@pytest.mark.asyncio
async def test_broadcast_failure_keeps_durable_notice(deleg_db, monkeypatch, caplog):
    from host_access import browser_reporting
    seed_audience(deleg_db)
    monkeypatch.setattr(browser_reporting, 'broadcast_message_rows', AsyncMock(side_effect=RuntimeError('private-info')))
    notice = await browser_reporting.report_cleanup('hidden')
    assert deleg_db.execute('SELECT COUNT(*) FROM messages WHERE id=?', (notice[1],)).fetchone()[0] == 1
    assert 'durable notice retained' in caplog.text and 'private-info' not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_command_reports_unprovable_cleanup(synthetic_browser, monkeypatch):
    from host_access import browser_cli, browser_reporting
    report = AsyncMock(return_value=('thread', 'notice'))
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    monkeypatch.setattr(owner, '_invoke', AsyncMock(side_effect=browser_cli.CommandCancelled(
        sessions.CommandResult(-1, '', '', completion='unknown'))))
    with pytest.raises(asyncio.CancelledError):
        await owner.run('thread', ['snapshot'])
    assert owner.live_count() == 1 and report.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['command', 'close'])
@pytest.mark.parametrize('recovery', ['pending', 'operator_required'])
async def test_tool_preserves_cleanup_disposition(monkeypatch, method, recovery):
    from tools import browser, browser_runtime
    monkeypatch.setattr(browser_runtime, '_check_installed', lambda: None)
    failure = sessions.BrowserSessionCleanupError('private-path', recovery=recovery)
    name = 'run_browser' if method == 'command' else 'close_browser'
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, name, AsyncMock(side_effect=failure))
    result = await browser._run(['snapshot'], thread_id='thread') if method == 'command' else await browser.BrowserCloseTool().execute(_thread_id='thread')
    assert result == failure.tool_message and 'private-path' not in result
    assert ('inform the user' in result) == (recovery == 'operator_required')
