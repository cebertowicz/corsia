"""Regression evidence through actual subprocess outcomes, never invented no-launch facts."""
import asyncio
import sys

import psutil
import pytest
from host_access import browser_cli as cli
from host_access import browser_identity as identity
from host_access import browser_sessions as sessions
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


async def test_real_client_rejection_before_daemon_does_not_strand_admission(
    synthetic_browser, monkeypatch,
):
    owner = synthetic_browser.owner
    await owner._pin_native()

    async def rejected(argv, *, timeout):
        return await cli.invoke(
            (sys.executable, '-c', 'import sys; print(\'{"success":false,"error":"Unknown command","errorType":"unknown_command"}\'); sys.exit(1)'),
            socket_dir=owner.socket_dir, timeout=timeout,
        )

    monkeypatch.setattr(owner, '_invoke', rejected)
    assert 'failed' in await owner.run('thread', ['snapshot'])
    assert owner.live_count() == 0
    await owner.shutdown()
    await owner.shutdown()


async def test_missing_client_preserves_healthy_bound_session(synthetic_browser, tmp_path):
    owner = synthetic_browser.owner
    await owner.run('thread', ['snapshot'])
    runtime = owner._sessions['thread']
    before = runtime.record
    owner.binary = str(tmp_path / 'missing')
    with pytest.raises(sessions.BrowserSetupError):
        await owner.run('thread', ['snapshot'])
    assert runtime.record == before


async def test_actual_client_wait_failure_keeps_safe_diagnostic(
    monkeypatch, tmp_path, caplog,
):
    original = asyncio.create_subprocess_exec
    clients = []

    async def broken_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        clients.append(process)

        real_wait = process.wait

        async def broken_wait():
            monkeypatch.setattr(process, 'wait', real_wait)
            raise OSError('private path /secret and session value')

        monkeypatch.setattr(process, 'wait', broken_wait)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', broken_spawn)
    result = await cli.invoke(
        (sys.executable, '-c', 'import time; time.sleep(20)'),
        socket_dir=tmp_path, timeout=3,
    )
    assert clients[0].returncode is not None
    assert 'phase=communicate; cause=os_error' in result.stderr
    assert result.stderr in caplog.text
    assert 'secret' not in caplog.text + repr(result)


async def test_connected_foreign_peer_receives_no_close_bytes(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    await fake.owner.run('foreign', ['snapshot'])
    foreign = fake.owner._sessions['foreign'].record
    reader, writer = await fake.connect(str(fake.owner.socket_dir / f'{foreign.session}.sock'))
    writes = []
    original_write = writer.write

    def track(data):
        writes.append(data)
        original_write(data)

    async def substituted(path, **kwargs):
        return reader, writer

    monkeypatch.setattr(writer, 'write', track)
    monkeypatch.setattr(asyncio, 'open_unix_connection', substituted)
    fake.owner.close_timeout = 0.05
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.close_thread('thread')
    assert not writes


async def test_exited_client_with_delayed_detached_daemon_remains_owned(
    synthetic_browser, monkeypatch, tmp_path,
):
    fake = synthetic_browser
    owner = fake.owner
    await owner._pin_native()
    spawned_pid = tmp_path / 'spawned-pid'
    release_daemon = tmp_path / 'release-daemon'
    daemon = '''
import os, pathlib, signal, sys, time
root, session, release = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
paths = [root / (session + suffix) for suffix in ('.pid', '.version', '.sock')]
def stop(*args):
    for path in paths:
        path.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
deadline = time.monotonic() + 30
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not release.exists():
    stop()
paths[0].write_text(str(os.getpid()))
paths[1].write_text('0.27.0')
paths[2].touch()
time.sleep(30)
stop()
'''
    client = '''
import os, pathlib, subprocess, sys
code, root, session, marker, release = sys.argv[1:]
env = dict(os.environ, AGENT_BROWSER_DAEMON='1', AGENT_BROWSER_SESSION=session)
child = subprocess.Popen([sys.executable, '-c', code, root, session, release], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
pathlib.Path(marker).write_text(str(child.pid))
print('{"success":false,"error":"Daemon failed to start"}')
sys.exit(1)
'''

    async def delayed(argv, *, timeout):
        session = argv[argv.index('--session') + 1]
        return await cli.invoke((sys.executable, '-c', client, daemon, str(owner.socket_dir),
                                 session, str(spawned_pid), str(release_daemon)), socket_dir=owner.socket_dir, timeout=EXIT_TIMEOUT)

    monkeypatch.setattr(owner, '_invoke', delayed)
    pid = None
    try:
        with pytest.raises(sessions.BrowserSessionCleanupError, match='startup'):
            await owner.run('thread', ['snapshot'])
        pid = int(spawned_pid.read_text())
        runtime = owner._sessions['thread']
        assert runtime.attempt.completion == 'exited' and runtime.record.binding is None
        assert owner.live_count() == 1
        # Release the daemon only after the owner has retained the unresolved startup.
        release_daemon.touch()
        # The daemon writes .sock last; wait for it rather than for a fixed time (#2852).
        last_artifact = owner.socket_dir / f'{runtime.record.session}.sock'
        deadline = asyncio.get_running_loop().time() + 10
        while not last_artifact.exists():
            assert asyncio.get_running_loop().time() < deadline, 'delayed daemon never wrote its artifacts'
            await asyncio.sleep(0.05)
        observed = await owner._observe(runtime)
        assert observed.alive and observed.binding.pid == pid
        assert runtime.record.binding == observed.binding
    finally:
        if spawned_pid.exists():
            try:
                process = identity.process_for_identity(int(spawned_pid.read_text()))
                process.terminate()
                await asyncio.to_thread(process.wait, EXIT_TIMEOUT)
            except (identity.BrowserPidAbsent, psutil.NoSuchProcess):
                pass
