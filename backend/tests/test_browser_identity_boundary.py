"""Real process identity and adversarial artifact validation."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import stat
import sys
import time

import psutil
import pytest
from host_access import browser_identity as identity, browser_sessions as sessions
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


@pytest.mark.parametrize('key', ['AGENT_BROWSER_DAEMON', 'AGENT_BROWSER_SESSION',
                                 'AGENT_BROWSER_SOCKET_DIR'])
@pytest.mark.parametrize('value', ['', 'wrong'])
async def test_wrong_or_missing_real_daemon_environment(synthetic_browser, key, value):
    fake = synthetic_browser
    fake.env_changes[key] = value
    with pytest.raises(sessions.BrowserSessionCleanupError, match='environment identity changed'):
        await fake.owner.run('thread', ['snapshot'])
    child = next(iter(fake.children.values()))
    assert child.poll() is None
    assert fake.owner.live_count() == 1
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.close_thread('thread')
    assert child.poll() is None


async def test_denied_environment_fails_closed(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    original = psutil.Process.environ
    pid = fake.owner._sessions['thread'].record.pid
    def denied(process):
        if process.pid == pid:
            raise psutil.AccessDenied(pid)
        return original(process)
    monkeypatch.setattr(psutil.Process, 'environ', denied)
    with pytest.raises(sessions.BrowserSessionCleanupError, match='environment identity is unavailable'):
        await fake.owner.close_thread('thread')
    assert next(iter(fake.children.values())).poll() is None
    assert len(fake.calls) == 1


async def test_process_predating_admission_cannot_bind(synthetic_browser):
    fake = synthetic_browser
    runtime = await fake.owner._admit('thread', lease_id=None)
    await fake.owner._pin_native()
    child = fake.spawn(runtime.record.session)
    await asyncio.to_thread(child.stdout.readline)
    await asyncio.sleep(identity.TIME_TOLERANCE_SECONDS + 0.15)
    runtime.record = replace(runtime.record, admitted_at=time.time())
    runtime.attempt_started_at = time.time()
    runtime.attempt = sessions.CommandResult(0, '', '')
    # A fresh locator pointing at an old daemon cannot confer ownership.
    (fake.owner.socket_dir / f'{runtime.record.session}.pid').write_text(str(child.pid))
    with pytest.raises(sessions.BrowserSessionCleanupError, match='stale'):
        await fake.owner._observe(runtime)
    assert runtime.record.pid is None


async def test_first_bind_requires_actual_launched_attempt(synthetic_browser):
    fake = synthetic_browser
    runtime = await fake.owner._admit('thread', lease_id=None)
    await fake.owner._pin_native()
    child = fake.spawn(runtime.record.session)
    await asyncio.to_thread(child.stdout.readline)
    with pytest.raises(sessions.BrowserSessionCleanupError, match='launch was not verified'):
        await fake.owner._observe(runtime)
    assert runtime.record.pid is None


async def test_missing_client_after_binding_still_allows_cleanup(synthetic_browser, tmp_path):
    fake = synthetic_browser
    link = tmp_path / 'client'
    link.symlink_to(fake.owner.binary)
    fake.owner.binary = str(link)
    await fake.owner.run('thread', ['snapshot'])
    record = fake.owner._sessions['thread'].record
    link.unlink()
    with pytest.raises(sessions.BrowserSetupError):
        await fake.owner.run('thread', ['snapshot'])
    assert fake.owner._sessions['thread'].record.pid == record.pid
    assert fake.owner.live_count() == 1
    await fake.owner.close_thread('thread')
    assert fake.owner.live_count() == 0 and len(fake.calls) == 1


async def test_changed_version_blocks_commands_and_signals(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    record = fake.owner._sessions['thread'].record
    (fake.owner.socket_dir / f'{record.session}.version').write_text('0.28.0')
    with pytest.raises(sessions.BrowserSessionCleanupError, match='locator changed'):
        await fake.owner.run('thread', ['snapshot'])
    with pytest.raises(sessions.BrowserSessionCleanupError):
        await fake.owner.close_thread('thread')
    assert fake.children[record.session].poll() is None
    assert fake.owner.live_count() == 1 and len(fake.calls) == 1


@pytest.mark.parametrize('raw', ['0', '1', '-1', 'bad', '', '2junk', '2\n3'])
def test_invalid_pid_locator(raw):
    with pytest.raises(identity.BrowserIdentityError):
        identity.parse_pid_sidecar(raw)


@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'fifo', 'directory'])
def test_nonregular_sidecar_reads_are_bounded(tmp_path, kind):
    path = tmp_path / 'session.pid'
    if kind == 'symlink':
        target = tmp_path / 'target'
        target.write_text('1234')
        path.symlink_to(target)
    elif kind == 'dangling':
        path.symlink_to(tmp_path / 'missing')
    elif kind == 'fifo':
        os.mkfifo(path)
    else:
        path.mkdir()
    start = time.monotonic()
    with pytest.raises(identity.BrowserIdentityError):
        identity._read_regular_sidecar(path, 64)
    assert time.monotonic() - start < 0.5


def test_session_validation_and_dangling_artifacts(tmp_path):
    with pytest.raises(identity.BrowserIdentityError):
        identity.session_sidecar_path(tmp_path, '../escape', 'pid')
    path = identity.session_sidecar_path(tmp_path, 'opaque-session', 'sock')
    path.symlink_to(tmp_path / 'missing')
    assert not identity.session_absent(tmp_path, 'opaque-session')


def test_socket_type_rejects_regular_file_and_symlink(tmp_path):
    path = tmp_path / 'session.sock'
    path.touch()
    with pytest.raises(identity.BrowserIdentityError):
        identity.artifact_stamp(path, socket_file=True)
    assert identity.socket_artifact_mode(stat.S_IFSOCK | 0o600)
    assert not identity.socket_artifact_mode(stat.S_IFLNK | 0o777)


async def test_birth_fingerprint_change_refuses_signals(synthetic_browser, monkeypatch):
    """A PID reuse seam exercises the refusal while the original child stays live."""
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    monkeypatch.setattr(identity, 'creation_fingerprint', lambda pid: 'replacement')
    with pytest.raises(sessions.BrowserSessionCleanupError, match='identity changed'):
        await fake.owner.close_thread('thread')
    assert next(iter(fake.children.values())).poll() is None


async def test_stale_first_sidecar_rejected(synthetic_browser):
    fake = synthetic_browser
    runtime = await fake.owner._admit('thread', lease_id=None)
    await fake.owner._pin_native()
    child = fake.spawn(runtime.record.session)
    await asyncio.to_thread(child.stdout.readline)
    path = fake.owner.socket_dir / f'{runtime.record.session}.pid'
    old = time.time() - 60
    os.utime(path, (old, old))
    runtime.attempt_started_at = time.time()
    runtime.attempt = sessions.CommandResult(0, '', '')
    with pytest.raises(sessions.BrowserSessionCleanupError, match='stale'):
        await fake.owner._observe(runtime)


async def test_process_exit_during_introspection_is_observed_but_not_certified(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    runtime = fake.owner._sessions['thread']
    child = fake.children[runtime.record.session]

    def exited_during_read(*args, **kwargs):
        child.stdin.close()
        child.wait(timeout=EXIT_TIMEOUT)
        raise identity.BrowserIdentityError('browser daemon environment identity is unavailable')

    monkeypatch.setattr(identity, 'browser_process_identity', exited_during_read)
    observed = await fake.owner._observe(runtime)
    assert not observed.alive and observed.binding == runtime.record.binding
    # Fresh disappearance is a fact, not proof that the producer acknowledged close.
    with pytest.raises(sessions.BrowserSessionCleanupError, match='without close acknowledgement'):
        await fake.owner.close_thread('thread')
    assert fake.owner.live_count() == 1


@pytest.mark.parametrize(('lag', 'stale'), [(1.5, False), (3.0, True)])
async def test_new_daemon_start_may_read_older_than_it_is(synthetic_browser, monkeypatch, lag, stale):
    """Linux start times are whole-second quantized, so a fresh daemon can read over a second old."""
    fake = synthetic_browser
    runtime = await fake.owner._admit('thread', lease_id=None)
    await fake.owner._pin_native()
    runtime.attempt_started_at = time.time()
    child = fake.spawn(runtime.record.session)
    await asyncio.to_thread(child.stdout.readline)
    runtime.attempt = sessions.CommandResult(0, '', '')
    measured = identity.process_create_time
    monkeypatch.setattr(identity, 'process_create_time', lambda process: measured(process) - lag)
    if stale:
        with pytest.raises(sessions.BrowserSessionCleanupError, match='stale'):
            await fake.owner._observe(runtime)
    else:
        assert (await fake.owner._observe(runtime)).alive
