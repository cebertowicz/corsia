"""Real process ownership and producer close, with only CLI/socket transport seams."""
import asyncio
import os
import signal

import psutil
import pytest
from host_access import browser_sessions as sessions
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


async def test_stable_startup_sidecars_survive_delayed_reuse(synthetic_browser, monkeypatch):
    from host_access import browser_launch
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    fake = synthetic_browser
    assert await fake.owner.run('thread', ['open', 'about:blank']) == 'ok'
    record = fake.owner._sessions['thread'].record
    path = fake.owner.socket_dir / f'{record.session}.pid'
    stamp = path.stat().st_mtime_ns
    await asyncio.sleep(1.15)
    assert await fake.owner.run('thread', ['snapshot']) == 'ok'
    assert fake.owner._sessions['thread'].record == record
    assert path.stat().st_mtime_ns == stamp
    await fake.owner.shutdown()


async def test_failed_navigation_binds_and_reuses_startup_files(synthetic_browser):
    fake = synthetic_browser
    fake.result = sessions.CommandResult(1, '', 'navigation failed')
    assert 'failed' in await fake.owner.run('thread', ['open', 'https://example.test/'])
    record = fake.owner._sessions['thread'].record
    assert record.pid and fake.owner.live_count() == 1
    fake.result = sessions.CommandResult(0, 'ok', '')
    assert await fake.owner.run('thread', ['snapshot']) == 'ok'
    assert fake.owner._sessions['thread'].record == record
    await fake.owner.shutdown()


async def test_close_has_no_cli_calls_and_proves_absence(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    record = fake.owner._sessions['thread'].record
    assert await fake.owner.close_thread('thread') == 'Browser session closed.'
    assert len(fake.calls) == 1 and fake.owner.live_count() == 0
    assert not list(fake.owner.socket_dir.glob(record.session + '.*'))


@pytest.mark.parametrize('mode', ['thread', 'lease', 'shutdown', 'timeout'])
async def test_all_teardown_routes_without_cli(synthetic_browser, mode):
    fake = synthetic_browser
    if mode == 'timeout':
        fake.result = sessions.CommandResult(-1, '', '', timed_out=True, completion='interrupted')
    result = await fake.owner.run('a', ['snapshot'], lease_id='lease-a')
    if mode == 'thread':
        await fake.owner.close_thread('a')
    elif mode == 'lease':
        await fake.owner.run('b', ['snapshot'], lease_id='lease-b')
        await fake.owner.terminate_lease('lease-a')
        assert fake.owner.live_count() == 1
        await fake.owner.shutdown()
    elif mode == 'shutdown':
        await fake.owner.shutdown()
    else:
        assert 'timed out' in result
    assert fake.owner.live_count() == 0
    assert len(fake.calls) == (2 if mode == 'lease' else 1)


@pytest.mark.parametrize('suffix', ['pid', 'version', 'sock'])
@pytest.mark.parametrize('mutation', ['remove', 'replace'])
async def test_changed_locator_blocks_before_effect(synthetic_browser, suffix, mutation):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    record = fake.owner._sessions['thread'].record
    path = fake.owner.socket_dir / f'{record.session}.{suffix}'
    path.rename(path.with_suffix('.old'))
    if mutation == 'replace':
        path.write_text('2')
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.run('thread', ['snapshot'])
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.close_thread('thread')
    assert len(fake.calls) == 1 and fake.children[record.session].poll() is None


@pytest.mark.parametrize('suffix', ['pid', 'version', 'sock', 'port', 'stream'])
async def test_preexisting_partial_artifacts_block_before_launch(synthetic_browser, suffix):
    fake = synthetic_browser
    await fake.owner._pin_native()
    runtime = await fake.owner._admit('thread', lease_id=None)
    (fake.owner.socket_dir / f'{runtime.record.session}.{suffix}').touch()
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.run('thread', ['snapshot'])
    assert not fake.calls and fake.owner.live_count() == 1


async def test_replacement_after_command_never_rebinds(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    record = fake.owner._sessions['thread'].record

    async def replace_daemon(argv, *, timeout):
        child = fake.children[record.session]
        child.stdin.close()
        await asyncio.to_thread(child.wait, EXIT_TIMEOUT)
        replacement = fake.spawn(record.session)
        await asyncio.to_thread(replacement.stdout.readline)
        return sessions.CommandResult(0, 'ok', '')

    monkeypatch.setattr(fake.owner, '_invoke', replace_daemon)
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.run('thread', ['snapshot'])
    assert fake.owner._sessions['thread'].record.pid == record.pid
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.close_thread('thread')
    assert fake.children[record.session].poll() is None


async def test_slow_close_retains_producer_then_recovers_and_reopens(synthetic_browser, monkeypatch):
    from host_access import browser_launch
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    fake = synthetic_browser
    fake.env_changes = {'TEST_CLOSE_DELAY': '0.3', 'TEST_CHILD': '1'}
    fake.owner.close_timeout = 0.05
    await fake.owner.run('thread', ['snapshot'])
    runtime = fake.owner._sessions['thread']
    record = runtime.record
    child_pid = int((fake.owner.socket_dir / f'{record.session}.child').read_text())
    assert os.getpgid(child_pid) == child_pid  # Browser owns a distinct kernel group.
    with pytest.raises(sessions.BrowserSessionCleanupError, match='pending'):
        await fake.owner.close_thread('thread')
    pending = runtime.close_task
    assert fake.children[record.session].poll() is None and psutil.pid_exists(child_pid)
    await asyncio.wait_for(asyncio.shield(pending), EXIT_TIMEOUT)
    await asyncio.to_thread(fake.children[record.session].wait, EXIT_TIMEOUT)
    assert await fake.owner.close_thread('thread') == 'Browser session closed.'
    assert runtime.close_task is pending and fake.owner.live_count() == 0
    fake.env_changes = {}
    assert await fake.owner.run('thread', ['snapshot']) == 'ok'
    fake.owner.close_timeout = EXIT_TIMEOUT
    await fake.owner.shutdown()
    await fake.owner.shutdown()


async def test_unknown_forced_death_never_certifies_unseen_child_absence(synthetic_browser):
    fake = synthetic_browser
    fake.env_changes = {'TEST_CHILD': '1'}
    await fake.owner.run('thread', ['snapshot'])
    record = fake.owner._sessions['thread'].record
    child_pid = int((fake.owner.socket_dir / f'{record.session}.child').read_text())
    daemon = fake.children[record.session]
    daemon.kill()
    await asyncio.to_thread(daemon.wait, EXIT_TIMEOUT)
    try:
        for path in fake.owner.socket_dir.glob(record.session + '.*'):
            path.unlink()
        for _ in range(2):
            with pytest.raises(sessions.BrowserSessionCleanupError, match='without close acknowledgement'):
                await fake.owner.close_thread('thread')
        assert fake.owner.live_count() == 1 and psutil.pid_exists(child_pid)
    finally:
        # Test-created exact child only; CORSIA never signals descendants here.
        os.kill(child_pid, signal.SIGTERM)


@pytest.mark.parametrize('missing', ['pid', 'version', 'sock'])
async def test_partial_launch_missing_startup_artifacts(synthetic_browser, monkeypatch, missing):
    fake = synthetic_browser
    await fake.owner._pin_native()

    async def partial(argv, *, timeout):
        result = await fake.invoke(argv, timeout=timeout)
        session = argv[argv.index('--session') + 1]
        (fake.owner.socket_dir / f'{session}.{missing}').unlink()
        return result

    monkeypatch.setattr(fake.owner, '_invoke', partial)
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.run('thread', ['snapshot'])
    assert fake.owner._sessions['thread'].record.pid is None
    assert next(iter(fake.children.values())).poll() is None
