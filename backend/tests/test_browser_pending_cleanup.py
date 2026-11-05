"""Compose real CLI pending-spawn outcomes with already-bound owner cleanup.

Only spawn scheduling/the harmless client executable and the shared fixture's
explicit initial CLI/socket-type seams are substituted. Identity checks stay real.
"""
import asyncio
import sys

import psutil
import pytest
from host_access import browser_cli as cli, browser_identity as identity, browser_sessions as sessions
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


async def cleanup(owner, route):
    if route == 'thread':
        await owner.close_thread('thread')
    elif route == 'lease':
        await owner.terminate_lease('lease')
    else:
        await owner.shutdown()


@pytest.mark.parametrize('route', ['thread', 'lease', 'shutdown'])
@pytest.mark.parametrize('outcome', ['timeout', 'cancel'])
async def test_bound_pending_spawn_recovers_after_late_reap(
    synthetic_browser, monkeypatch, route, outcome,
):
    fake = synthetic_browser
    owner = fake.owner
    await owner.run('thread', ['snapshot'], lease_id='lease')
    runtime = owner._sessions['thread']
    record = runtime.record
    original_spawn = asyncio.create_subprocess_exec
    entered, release, spawned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pending, clients = [], []

    async def delayed_spawn(*args, **kwargs):
        pending.append(asyncio.current_task())
        entered.set()
        await release.wait()
        process = await original_spawn(*args, **kwargs)
        clients.append(process)
        spawned.set()
        return process

    async def actual_cli(argv, *, timeout):
        return await cli.invoke(
            (sys.executable, '-c', 'import time;time.sleep(30)'),
            socket_dir=owner.socket_dir, timeout=timeout,
        )

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    monkeypatch.setattr(owner, '_invoke', actual_cli)
    attempt = asyncio.create_task(owner.run(
        'thread', ['snapshot'], timeout=0.02 if outcome == 'timeout' else 30,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if outcome == 'cancel':
            attempt.cancel()
            # Exercise invoke's real five-second pending-spawn cancellation wait.
            with pytest.raises(cli.CommandCancelled):
                await asyncio.wait_for(attempt, 7)
        else:
            assert 'still running' in await attempt
        assert runtime.attempt.completion == 'unknown' and not pending[0].done()
        assert owner.live_count() == 1

        with pytest.raises(sessions.BrowserSessionCleanupError):
            await cleanup(owner, route)
        assert owner._sessions['thread'] is runtime
        assert runtime.record.pid == record.pid
        assert runtime.record.terminal_state == 'resetting'
        assert fake.children[record.session].poll() is None
        assert not pending[0].done() and owner.live_count() == 1

        release.set()
        await asyncio.wait_for(spawned.wait(), EXIT_TIMEOUT)
        await asyncio.wait_for(clients[0].wait(), EXIT_TIMEOUT)
        assert clients[0].returncode is not None
        # Settled client plus the same owned daemon permits producer-mediated close.
        await asyncio.wait_for(runtime.attempt.settlement, EXIT_TIMEOUT)
        if route == 'thread':
            with pytest.raises(sessions.BrowserSessionCleanupError) as blocked:
                await owner.run('thread', ['snapshot'])
            assert blocked.value.recovery == 'pending'
            assert runtime.record.terminal_state == 'resetting'
            assert runtime.report is None
        await cleanup(owner, route)
        assert runtime.attempt.completion == 'interrupted' and owner.live_count() == 0
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), EXIT_TIMEOUT)
        for client in clients:
            await asyncio.wait_for(client.wait(), EXIT_TIMEOUT)


def _unavailable_during_exit(source, *, typed=True):
    error_type = identity.BrowserExitTornRead if typed else identity.BrowserIdentityError
    cause = error_type('browser session artifact is unavailable')
    cause.__cause__ = source
    return cause


@pytest.mark.parametrize(('cause', 'retry'), [
    (identity.BrowserPidAbsent(), True),
    (_unavailable_during_exit(FileNotFoundError()), True),
    (_unavailable_during_exit(psutil.AccessDenied()), True),
    (_unavailable_during_exit(FileNotFoundError(), typed=False), False),
    (identity.BrowserIdentityChanged('changed'), False),
    (identity.BrowserIdentityError('browser daemon environment identity changed'), False),
    (identity.BrowserIdentityError('browser daemon executable identity changed'), False),
    (identity.BrowserIdentityError('browser socket directory is not private'), False),
])
async def test_acknowledged_close_reobserves_only_exit_torn_read(synthetic_browser, monkeypatch, cause, retry):
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    observe, injected = owner._observe, []

    async def exiting(target, peer_socket=None):
        if not injected and peer_socket is None and target.close_task is not None and target.close_task.done():
            injected.append(True)
            failure = sessions.BrowserSessionCleanupError(str(cause))
            failure.__cause__ = cause
            raise failure
        return await observe(target, peer_socket)

    monkeypatch.setattr(owner, '_observe', exiting)
    if retry:
        await owner.close_thread('thread')
        assert owner.live_count() == 0 and runtime.record.terminal_state == 'closed'
    else:
        with pytest.raises(sessions.BrowserSessionCleanupError):
            await owner.close_thread('thread')
        assert owner._sessions['thread'] is runtime and runtime.record.terminal_state == 'death_unverified'
    assert injected


def test_exit_torn_read_type_is_emitted_only_for_specific_missing_reads(tmp_path):
    with pytest.raises(identity.BrowserExitTornRead):
        identity.artifact_stamp(tmp_path / 'missing')
    with pytest.raises(identity.BrowserExitTornRead):
        identity.read_regular_sidecar(tmp_path / 'missing', 64)
    with pytest.raises(identity.BrowserIdentityError) as malformed:
        identity.artifact_stamp(tmp_path)
    assert not isinstance(malformed.value, identity.BrowserExitTornRead)

    class DeniedProcess:
        def environ(self):
            raise psutil.AccessDenied()

    with pytest.raises(identity.BrowserExitTornRead):
        identity.daemon_environment_matches(
            DeniedProcess(), session='session', socket_dir=tmp_path,
        )

    # Linux serves an empty environment once an exiting process has released its memory.
    class EmptyEnvProcess:
        def environ(self):
            return {}

    with pytest.raises(identity.BrowserExitTornRead):
        identity.daemon_environment_matches(
            EmptyEnvProcess(), session='session', socket_dir=tmp_path,
        )

    # macOS returns an empty or denied executable path while a process exits, before it is a zombie.
    class ExitingProcess:
        def __init__(self, exe):
            self._exe = exe

        def exe(self):
            if isinstance(self._exe, BaseException):
                raise self._exe
            return self._exe

    for exiting in ('', psutil.AccessDenied()):
        with pytest.raises(identity.BrowserExitTornRead):
            identity.executable_fingerprint_for_process(ExitingProcess(exiting))
    with pytest.raises(identity.BrowserIdentityError) as unreadable:
        identity.executable_fingerprint_for_process(ExitingProcess(OSError('io')))
    assert not isinstance(unreadable.value, identity.BrowserExitTornRead)


async def test_acknowledged_close_retry_survives_torn_pre_observation(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    fake.env_changes['TEST_CLOSE_DELAY'] = '0.2'
    owner = fake.owner
    owner.close_timeout = 0.01
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
        await owner.close_thread('thread')
    assert caught.value.recovery == 'pending'
    assert runtime.close_task is not None
    await runtime.close_task  # The producer acknowledged close before the retry.

    observe, injected = owner._observe, []

    async def exiting(target, peer_socket=None):
        if not injected and peer_socket is None:
            injected.append(True)
            failure = sessions.BrowserSessionCleanupError('browser pid disappeared during exit')
            failure.__cause__ = identity.BrowserPidAbsent()
            raise failure
        return await observe(target, peer_socket)

    monkeypatch.setattr(owner, '_observe', exiting)
    owner.close_timeout = EXIT_TIMEOUT
    await owner.close_thread('thread')
    assert injected and owner.live_count() == 0 and runtime.record.terminal_state == 'closed'


async def test_retry_waits_for_in_flight_close_while_daemon_exits(synthetic_browser, monkeypatch):
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    release, request = asyncio.Event(), sessions.browser_cli.close_request

    async def held(*args):
        await release.wait()
        return await request(*args)

    monkeypatch.setattr(sessions.browser_cli, 'close_request', held)
    owner.close_timeout = 0.01
    with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
        await owner.close_thread('thread')
    assert caught.value.recovery == 'pending' and not runtime.close_task.done()
    observe = owner._observe

    async def exiting(target, peer_socket=None):
        # An acknowledged daemon removes its sidecars one by one while it exits (#2351).
        if peer_socket is None and not target.close_task.done():
            failure = sessions.BrowserSessionCleanupError('browser session artifact is unavailable')
            failure.__cause__ = identity.BrowserExitTornRead('browser session artifact is unavailable')
            raise failure
        return await observe(target, peer_socket)

    monkeypatch.setattr(owner, '_observe', exiting)
    owner.close_timeout = EXIT_TIMEOUT
    retry = asyncio.create_task(owner.close_thread('thread'))
    for _ in range(3):
        await asyncio.sleep(0)
    release.set()
    assert await retry == 'Browser session closed.'
    assert owner.live_count() == 0 and runtime.record.terminal_state == 'closed'


async def test_pid_sidecar_removed_mid_read_after_ack_is_an_exit_torn_read(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    fake.env_changes['TEST_HOLD_EXIT'] = '1'
    owner = fake.owner
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    runtime.close_task = asyncio.create_task(owner._request_close(runtime))
    await runtime.close_task
    read, removed = identity.read_regular_sidecar, []

    def exiting(path, limit):
        if not removed and path.suffix == '.pid':
            removed.append(True)
            path.unlink()  # The acknowledged daemon removes its pid after the stamp, before the read (#2351).
            fake.release_exit(runtime.record.session)
        return read(path, limit)

    monkeypatch.setattr(identity, 'read_regular_sidecar', exiting)
    await owner.close_thread('thread')
    assert removed and owner.live_count() == 0 and runtime.record.terminal_state == 'closed'


async def test_persistent_torn_read_after_ack_requires_operator_recovery(synthetic_browser, monkeypatch):
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    runtime.close_task = asyncio.create_task(owner._request_close(runtime))
    await runtime.close_task
    observe = owner._observe

    async def unavailable(*args, **kwargs):
        failure = sessions.BrowserSessionCleanupError('browser pid disappeared during exit')
        failure.__cause__ = identity.BrowserPidAbsent()
        raise failure

    monkeypatch.setattr(owner, '_observe', unavailable)
    owner.close_timeout = 0.01
    with pytest.raises(sessions.BrowserSessionCleanupError) as caught:
        await owner.close_thread('thread')
    assert caught.value.recovery == 'operator_required'
    assert runtime.record.terminal_state == 'death_unverified'
    monkeypatch.setattr(owner, '_observe', observe)
    owner.close_timeout = EXIT_TIMEOUT
    await owner.close_thread('thread')
    assert owner.live_count() == 0


async def test_empty_shutdown_drops_historical_failure_after_cleanup_succeeds(
    synthetic_browser, tmp_path,
):
    fake = synthetic_browser
    owner = fake.owner
    await owner.run('thread', ['snapshot'])
    owner.binary = str(tmp_path / 'missing-client')
    with pytest.raises(sessions.BrowserSetupError):
        await owner.run('thread', ['snapshot'])
    assert owner._sessions['thread'].record.terminal_state == 'running'
    await owner.close_thread('thread')
    assert owner.live_count() == 0 and not owner._sessions
    for _ in range(2):
        await owner.shutdown()


async def test_definite_failed_spawn_preserves_binding_and_allows_known_cleanup(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    runtime = fake.owner._sessions['thread']
    record = runtime.record
    fake.result = sessions.CommandResult(1, '', '')
    assert 'failed' in await fake.owner.run('thread', ['snapshot'])
    assert runtime.record == record and fake.owner.live_count() == 1
    await fake.owner.close_thread('thread')
    await fake.owner.shutdown()
    assert fake.owner.live_count() == 0


async def test_empty_executable_read_after_close_ack_retries_to_closed(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    # Held so the post-ack poll reaches a live daemon; an early exit would skip the torn read.
    fake.env_changes['TEST_HOLD_EXIT'] = '1'
    owner = fake.owner
    await owner.run('thread', ['snapshot'])
    session = owner._sessions['thread'].record.session
    real_exe, real_close = psutil.Process.exe, owner._request_close
    acked, torn = [], []

    async def close(runtime):
        await real_close(runtime)
        acked.append(True)

    def exe(self):
        if acked and not torn and self.pid != psutil.Process().pid:
            torn.append(self.pid)
            fake.release_exit(session)
            return ''
        return real_exe(self)

    monkeypatch.setattr(owner, '_request_close', close)
    monkeypatch.setattr(psutil.Process, 'exe', exe)
    await owner.close_thread('thread')
    assert torn and owner.live_count() == 0


async def test_empty_environment_read_after_close_ack_retries_to_closed(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    fake.env_changes['TEST_HOLD_EXIT'] = '1'  # Same hold as the executable case above.
    owner = fake.owner
    await owner.run('thread', ['snapshot'])
    session = owner._sessions['thread'].record.session
    real_environ, real_close = psutil.Process.environ, owner._request_close
    acked, torn = [], []

    async def close(runtime):
        await real_close(runtime)
        acked.append(True)

    def environ(self):
        if acked and not torn and self.pid != psutil.Process().pid:
            torn.append(self.pid)
            fake.release_exit(session)
            return {}
        return real_environ(self)

    monkeypatch.setattr(owner, '_request_close', close)
    monkeypatch.setattr(psutil.Process, 'environ', environ)
    await owner.close_thread('thread')
    assert torn and owner.live_count() == 0
