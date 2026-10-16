"""Native admission and actual subprocess attempt outcomes, without a browser."""
import asyncio
import os
from pathlib import Path
import sys

import pytest
from host_access import browser_cli as cli, browser_identity as identity
from host_access import browser_sessions as sessions
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


@pytest.mark.parametrize('name,body', [('agent-browser.js', '#!/usr/bin/env node\n'),
                                      ('agent-browser', '#!/bin/sh\n'),
                                      ('launcher.py', '#!/usr/bin/env python\n'),
                                      ('wrapper.bin', 'not native')])
async def test_all_script_wrappers_refused_before_any_invocation(synthetic_browser, tmp_path, monkeypatch, name, body):
    fake = synthetic_browser
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    fake.owner.binary = str(path)
    async def forbidden(*args, **kwargs):
        pytest.fail('wrapper was invoked')
    monkeypatch.setattr(fake.owner, '_invoke', forbidden)
    with pytest.raises(sessions.BrowserSetupError, match='native agent-browser'):
        await fake.owner.run('thread', ['snapshot'])
    assert fake.owner.live_count() == 0


def _chrome_folders(tmp_path, monkeypatch, *, system: bool, user: bool, executable: bool = True):
    folders = (tmp_path / 'Applications', tmp_path / 'home' / 'Applications')
    for folder, present in zip(folders, (system, user)):
        if present:
            binary = folder / 'Google Chrome.app/Contents/MacOS/Google Chrome'
            binary.parent.mkdir(parents=True)
            binary.write_text('stub')
            binary.chmod(0o755 if executable else 0o644)
    monkeypatch.setattr(cli, '_application_dirs', lambda: folders)
    monkeypatch.delenv('AGENT_BROWSER_EXECUTABLE_PATH', raising=False)
    return [str(folder / 'Google Chrome.app/Contents/MacOS/Google Chrome') for folder in folders]


def test_chrome_executable_prefers_the_environment(tmp_path, monkeypatch):
    _chrome_folders(tmp_path, monkeypatch, system=True, user=True)
    monkeypatch.setenv('AGENT_BROWSER_EXECUTABLE_PATH', '/opt/chrome')
    assert cli.chrome_executable() == ('/opt/chrome', 'env')


def test_chrome_executable_uses_system_chrome_before_the_user_copy(tmp_path, monkeypatch):
    system, _user = _chrome_folders(tmp_path, monkeypatch, system=True, user=True)
    assert cli.chrome_executable() == (system, 'system')


def test_chrome_executable_falls_back_to_the_user_applications_copy(tmp_path, monkeypatch):
    _system, user = _chrome_folders(tmp_path, monkeypatch, system=False, user=True)
    assert cli.chrome_executable() == (user, 'system')


@pytest.mark.parametrize('installed', [False, True])
def test_chrome_executable_keeps_the_agent_browser_default_without_a_runnable_chrome(tmp_path, monkeypatch, installed):
    _chrome_folders(tmp_path, monkeypatch, system=installed, user=False, executable=False)
    assert cli.chrome_executable() == (None, 'default')


def test_canonical_native_and_symlink_resolve_same_identity(tmp_path):
    link = tmp_path / 'agent-browser'
    link.symlink_to(sys.executable)
    assert cli.resolve_native(str(link)) == cli.resolve_native(sys.executable)
    assert Path(cli.resolve_native(str(link))[0]).is_absolute()


async def test_actual_unsupported_native_version_is_refused(synthetic_browser, monkeypatch):
    owner = synthetic_browser.owner
    # Use the real client boundary against Python, a native unsupported executable.
    monkeypatch.setenv('AGENT_BROWSER_DAEMON', '1')
    monkeypatch.setattr(owner, '_invoke', sessions.BrowserSessionOwner._invoke.__get__(owner))
    with pytest.raises(sessions.BrowserSetupError, match='supported native agent-browser'):
        await owner.run('thread', ['snapshot'])
    assert owner.live_count() == 0


async def test_symlink_drift_never_changes_pinned_target(synthetic_browser, tmp_path):
    fake = synthetic_browser
    link = tmp_path / 'agent-browser'
    link.symlink_to(fake.owner.binary)
    fake.owner.binary = str(link)
    await fake.owner.run('thread', ['snapshot'])
    pin = fake.owner._native
    link.unlink()
    link.symlink_to('/bin/sh')
    with pytest.raises(sessions.BrowserSetupError, match='executable identity changed'):
        await fake.owner.run('thread', ['snapshot'])
    assert fake.owner._native == pin
    assert len(fake.calls) == 1
    await fake.owner.close_thread('thread')


def test_headed_launch_option_keeps_the_local_mode_guard(tmp_path, monkeypatch):
    config = tmp_path / "agent-browser.json"
    config.write_text("{}")
    monkeypatch.setenv("AGENT_BROWSER_CONFIG", str(config))
    cli.check_local_mode(["--headed", "open", "about:blank"])


async def test_client_environment_removes_daemon_mode(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_BROWSER_DAEMON', '1')
    monkeypatch.setenv('AGENT_BROWSER_SESSION', 'inherited-session')
    monkeypatch.setenv('AGENT_BROWSER_DASHBOARD', '1')
    code = ("import os; assert 'AGENT_BROWSER_DAEMON' not in os.environ; "
            "assert 'AGENT_BROWSER_SESSION' not in os.environ; "
            "assert 'AGENT_BROWSER_DASHBOARD' not in os.environ; "
            "assert os.environ['AGENT_BROWSER_SOCKET_DIR']; print('ok')")
    result = await cli.invoke((sys.executable, '-c', code), socket_dir=tmp_path, timeout=EXIT_TIMEOUT)
    assert result == cli.CommandResult(0, 'ok', '')


@pytest.mark.parametrize('error,cause', [
    (FileNotFoundError('private-message'), 'not_found'),
    (PermissionError('private-message'), 'permission_error'),
    (OSError('private-message'), 'os_error'),
    (type('private_exception_class', (Exception,), {})('private-message'), 'unexpected_error'),
])
async def test_spawn_exception_class_cannot_prove_no_execution(monkeypatch, tmp_path, caplog, error, cause):
    async def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fail)
    result = await cli.invoke(('unused',), socket_dir=tmp_path, timeout=1)
    assert result.completion == 'unknown' and result.returncode != 0
    assert f'cause={cause}' in result.stderr
    assert result.stderr in caplog.text
    assert 'private' not in caplog.text + repr(result)


async def test_cancel_during_pending_spawn_exception_stays_unknown(monkeypatch, tmp_path, caplog):
    entered, release = asyncio.Event(), asyncio.Event()
    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise PermissionError('private-message /private/path')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fail)
    task = asyncio.create_task(cli.invoke(('unused',), socket_dir=tmp_path, timeout=3))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(cli.CommandCancelled) as caught:
        await task
    assert caught.value.result.completion == 'unknown'
    assert 'phase=late_spawn; cause=permission_error' in caplog.text
    assert caught.value.result.stderr in caplog.text
    assert 'private' not in caplog.text + repr(caught.value.result)


async def test_cancel_after_actual_spawn_reaps_client(monkeypatch, tmp_path):
    original = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    processes = []
    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        entered.set()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    task = asyncio.create_task(cli.invoke((sys.executable, '-c', 'import time;time.sleep(30)'),
                                          socket_dir=tmp_path, timeout=3))
    await entered.wait()
    task.cancel()
    with pytest.raises(cli.CommandCancelled) as caught:
        await task
    assert caught.value.result.completion == 'interrupted'
    assert processes[0].returncode is not None


async def test_timeout_reaps_actual_client(tmp_path):
    result = await cli.invoke((sys.executable, '-c', 'import time;time.sleep(30)'),
                              socket_dir=tmp_path, timeout=0.05)
    assert result.timed_out and result.completion == 'interrupted'


async def test_pending_spawn_timeout_remains_unknown_and_reaps_late_client(monkeypatch, tmp_path):
    original = asyncio.create_subprocess_exec
    release, spawned = asyncio.Event(), asyncio.Event()
    processes = []
    async def delayed(*args, **kwargs):
        await release.wait()
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
    result = await cli.invoke((sys.executable, '-c', 'import time;time.sleep(30)'),
                              socket_dir=tmp_path, timeout=0.02)
    assert result.timed_out and result.completion == 'unknown'
    release.set()
    await asyncio.wait_for(spawned.wait(), EXIT_TIMEOUT)
    await asyncio.wait_for(processes[0].wait(), EXIT_TIMEOUT)
    assert processes[0].returncode is not None


async def test_native_file_drift_blocked_without_reinspection(synthetic_browser, tmp_path):
    import shutil
    fake = synthetic_browser
    path = tmp_path / 'native'
    shutil.copy2(sys.executable, path)
    fake.owner.binary = str(path)
    await fake.owner._pin_native()
    pin = fake.owner._native
    path.chmod(0o700 if path.stat().st_mode & 0o777 != 0o700 else 0o755)
    with pytest.raises(sessions.BrowserSetupError, match='identity changed'):
        await fake.owner._pin_native()
    assert fake.owner._native == pin and not fake.calls
