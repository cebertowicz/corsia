"""Saved-profile recovery never signals another browser."""
import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from host_access import browser_chrome, browser_download, browser_orphan, browser_owner_lock, browser_session_launch, group_probe, processes
from host_access.browser_session_state import BrowserRuntime, BrowserSessionRecord, BrowserSessionError
from host_access.browser_sessions import BrowserSessionOwner


class Process:
    def __init__(self, pid, profile, ppid=1, headless=True):
        self.pid, self.profile, self.parent, self.headless = pid, profile, ppid, headless
        self.birth = 123.0

    def ppid(self):
        return self.parent

    def cmdline(self):
        return ['/Applications/Google Chrome', f'--user-data-dir={self.profile}', *(['--headless=new'] if self.headless else [])]

    def uids(self):
        return SimpleNamespace(real=os.getuid())

    def create_time(self):
        return self.birth

    def is_running(self):
        return True


async def test_sweep_closes_orphan_then_starts_and_preserves_other_holders(monkeypatch, caplog, tmp_path):
    profile = '/saved/profile'
    candidates = [Process(11, profile), Process(13, '/saved/profile-other')]
    events = []
    monkeypatch.setattr(browser_orphan.psutil, 'process_iter', lambda *_: candidates)
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(browser_orphan.identity, 'process_for_identity', lambda pid: candidates[0])
    def stop(pid, still_ours, *, on_signal, ignore_zombie_leader):
        assert ignore_zombie_leader is True
        assert still_ours() is True
        on_signal()
        events.append(('closed', pid))
        return 'stopped'
    monkeypatch.setattr(processes, 'stop_orphan_group', stop)
    monkeypatch.setattr(browser_session_launch.sys, 'platform', 'darwin')
    monkeypatch.setattr(browser_download, 'is_private_download', lambda _: True)
    chrome = SimpleNamespace(process=SimpleNamespace(pid=99), closed=False, release_stream=Mock())
    async def launch(*args, **kwargs):
        events.append(('launched', kwargs['profile']))
        return chrome
    monkeypatch.setattr(browser_chrome.ChromeProcess, 'launch', launch)
    monkeypatch.setattr(browser_session_launch, 'apply_kept_signins', AsyncMock())
    monkeypatch.setattr(browser_session_launch, 'verify_stream', AsyncMock())
    record = Mock()
    monkeypatch.setattr(browser_owner_lock, 'record_launched_chrome', record)
    runtime = BrowserRuntime(BrowserSessionRecord('new', 'session', profile=profile))
    owned = SimpleNamespace(chrome=SimpleNamespace(process=SimpleNamespace(pid=12), closed=False))
    owner = SimpleNamespace(_chrome='chrome', _user_agent=None, socket_dir=tmp_path,
                            _sessions={'new': runtime, 'owned': owned},
                            _invoke=AsyncMock(), _session_argv=lambda *_: (), _observe_attempt=AsyncMock())
    with caplog.at_level('INFO'):
        await browser_session_launch.start_chrome(owner, runtime)
    assert events == [('closed', 11), ('launched', profile)]
    record.assert_called_once_with(tmp_path, 'session', chrome, profile)
    assert 'pid=11 profile=profile' in caplog.text and profile not in caplog.text


@pytest.mark.parametrize('parent,owned', [(1, frozenset({12})), (999, frozenset()), (1, frozenset())])
def test_live_or_unowned_holder_is_not_touched(monkeypatch, parent, owned):
    process = Process(12, '/saved/profile', ppid=parent)
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [process])
    monkeypatch.setattr(os, 'getpgid', lambda pid: 200 if not owned and parent == 1 else pid)
    stop = Mock()
    monkeypatch.setattr(processes, 'stop_orphan_group', stop)
    with pytest.raises(browser_orphan.BrowserProfileInUse, match='process 12'):
        browser_orphan.close_orphans('/saved/profile', owned)
    stop.assert_not_called()


def test_live_holder_prevents_sweep_of_other_candidates(monkeypatch):
    candidates = [Process(11, '/saved/profile'), Process(12, '/saved/profile', ppid=999)]
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: candidates)
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    stop = Mock()
    monkeypatch.setattr(processes, 'stop_orphan_group', stop)
    with pytest.raises(browser_orphan.BrowserProfileInUse):
        browser_orphan.close_orphans('/saved/profile', frozenset())
    stop.assert_not_called()


def test_headed_orphan_left_for_person(monkeypatch):
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [Process(12, '/saved/profile', headless=False)])
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    stop = Mock()
    monkeypatch.setattr(processes, 'stop_orphan_group', stop)
    with pytest.raises(browser_orphan.BrowserProfileInUse, match='Please close that window'):
        browser_orphan.close_orphans('/saved/profile', frozenset())
    stop.assert_not_called()


@pytest.mark.parametrize('changed', ['birth', 'parent'])
def test_changed_identity_is_refused_before_signal(monkeypatch, changed):
    process = Process(12, '/saved/profile')
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [process])
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(processes, '_group_pids', lambda *_, **__: (12,))
    monkeypatch.setattr(browser_orphan.identity, 'process_for_identity', lambda pid: process)
    signals = Mock()
    monkeypatch.setattr(os, 'killpg', signals)
    original = processes.stop_orphan_group
    def change_then_stop(*args, **kwargs):
        setattr(process, changed, 999)
        return original(*args, **kwargs)
    monkeypatch.setattr(processes, 'stop_orphan_group', change_then_stop)
    with pytest.raises(browser_orphan.BrowserProfileInUse, match='process 12'):
        browser_orphan.close_orphans('/saved/profile', frozenset())
    signals.assert_not_called()


def test_reused_pid_uses_fresh_process_identity(monkeypatch):
    original = Process(12, '/saved/profile')
    replacement = Process(12, '/saved/profile')
    replacement.birth = 999.0
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [original])
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(processes, '_group_pids', lambda *_, **__: (12,))
    monkeypatch.setattr(browser_orphan.identity, 'process_for_identity', lambda pid: replacement)
    signals = Mock()
    monkeypatch.setattr(os, 'killpg', signals)
    with pytest.raises(browser_orphan.BrowserProfileInUse, match='process 12'):
        browser_orphan.close_orphans('/saved/profile', frozenset())
    signals.assert_not_called()


@pytest.mark.parametrize('method', ['uids', 'create_time'])
def test_access_denied_after_match_is_refused(monkeypatch, method):
    process = Process(12, '/saved/profile')
    monkeypatch.setattr(process, method, Mock(side_effect=psutil.AccessDenied(12)))
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [process])
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    stop = Mock()
    monkeypatch.setattr(processes, 'stop_orphan_group', stop)
    with pytest.raises(browser_orphan.BrowserProfileInUse, match='process 12'):
        browser_orphan.close_orphans('/saved/profile', frozenset())
    stop.assert_not_called()


def test_shared_stop_closes_real_process_group():
    child = subprocess.Popen(['sleep', '30'], start_new_session=True)
    reaper = threading.Thread(target=child.wait, daemon=True)
    reaper.start()
    try:
        assert processes.stop_orphan_group(child.pid, lambda: True) == 'stopped'
    finally:
        if child.poll() is None:
            child.kill()
        reaper.join(timeout=5)


def test_all_zombie_eperm_probe_is_gone(monkeypatch):
    leader = SimpleNamespace(pid=123, status=lambda: psutil.STATUS_ZOMBIE)
    zombie = SimpleNamespace(pid=124, status=lambda: psutil.STATUS_ZOMBIE)
    monkeypatch.setattr(os, 'killpg', Mock(side_effect=PermissionError()))
    monkeypatch.setattr(os, 'getpgid', lambda _: 123)
    monkeypatch.setattr(psutil, 'Process', lambda _: leader)
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [leader, zombie])
    assert group_probe.group_pids(123) == (123,)  # Existing managed owner must reap its leader.
    assert group_probe.group_pids(123, ignore_zombie_leader=True) == ()
    assert processes.stop_orphan_group(123, lambda: True, ignore_zombie_leader=True) == 'already_gone'
    zombie.status = lambda: psutil.STATUS_RUNNING
    assert group_probe.group_pids(123, ignore_zombie_leader=True) == (123,)


def test_unknown_startup_details_are_not_logged():
    assert browser_orphan.startup_message(RuntimeError('https://example.test/private')) == 'details withheld'
    for message in browser_orphan._SAFE_STARTUP_MESSAGES:
        assert browser_orphan.startup_message(RuntimeError(message)) == message


async def test_linux_launch_does_not_scan(monkeypatch):
    monkeypatch.setattr(browser_session_launch.sys, 'platform', 'linux')
    monkeypatch.setattr(browser_orphan, 'close_orphans', Mock(side_effect=AssertionError('scanned')))
    await browser_session_launch.start_chrome(SimpleNamespace(), SimpleNamespace())


@pytest.mark.parametrize('failure,message', [
    (RuntimeError(browser_chrome.CHROME_EXITED), 'managed browser startup failed; its processes were closed'),
    (browser_orphan.BrowserProfileInUse(12), 'process 12'),
])
async def test_launch_failure_warns_and_preserves_user_message(monkeypatch, caplog, failure, message):
    owner = BrowserSessionOwner(namespace='corsia-test')
    runtime = BrowserRuntime(BrowserSessionRecord('thread', 'session'))
    owner._sessions['thread'] = runtime
    monkeypatch.setattr(owner, '_observe', AsyncMock(return_value=SimpleNamespace(alive=False, artifacts_absent=True)))
    monkeypatch.setattr(owner, '_close_one', AsyncMock(return_value=None))
    monkeypatch.setattr(browser_session_launch, 'start_chrome', AsyncMock(side_effect=failure))
    with pytest.raises(BrowserSessionError, match=message):
        await owner._run_admitted(runtime, ['snapshot'], lease_id=None, timeout=1)
    assert f'{type(failure).__name__}: {failure}' in caplog.text
