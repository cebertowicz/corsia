"""Saved-profile recovery never signals another browser."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from host_access import browser_chrome, browser_download, browser_orphan, browser_session_launch
from host_access.browser_session_state import BrowserRuntime, BrowserSessionRecord, BrowserSessionError
from host_access.browser_sessions import BrowserSessionOwner


class Process:
    def __init__(self, pid, profile, ppid=1):
        self.pid, self.profile, self.parent = pid, profile, ppid

    def ppid(self):
        return self.parent

    def cmdline(self):
        return ['/Applications/Google Chrome', f'--user-data-dir={self.profile}']

    def uids(self):
        return SimpleNamespace(real=browser_orphan.os.getuid())

    def create_time(self):
        return 123.0

    def is_running(self):
        return True


async def test_sweep_closes_orphan_then_starts_and_preserves_other_holders(monkeypatch):
    profile = '/saved/profile'
    processes = [Process(11, profile), Process(13, '/saved/profile-other')]
    events = []
    monkeypatch.setattr(browser_orphan.psutil, 'process_iter', lambda *_: processes)
    monkeypatch.setattr(browser_orphan.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(browser_orphan, '_terminate_group', lambda pid: events.append(('closed', pid)))
    monkeypatch.setattr(browser_session_launch.sys, 'platform', 'darwin')
    monkeypatch.setattr(browser_download, 'is_private_download', lambda _: True)
    chrome = SimpleNamespace(process=SimpleNamespace(pid=99), closed=False, release_stream=Mock())
    async def launch(*args, **kwargs):
        events.append(('launched', kwargs['profile']))
        return chrome
    monkeypatch.setattr(browser_chrome.ChromeProcess, 'launch', launch)
    monkeypatch.setattr(browser_session_launch, 'apply_kept_signins', AsyncMock())
    monkeypatch.setattr(browser_session_launch, 'verify_stream', AsyncMock())
    runtime = BrowserRuntime(BrowserSessionRecord('new', 'session', profile=profile))
    owned = SimpleNamespace(chrome=SimpleNamespace(process=SimpleNamespace(pid=12), closed=False))
    owner = SimpleNamespace(_chrome='chrome', _user_agent=None, _sessions={'new': runtime, 'owned': owned},
                            _invoke=AsyncMock(), _session_argv=lambda *_: (), _observe_attempt=AsyncMock())
    await browser_session_launch.start_chrome(owner, runtime)
    assert events == [('closed', 11), ('launched', profile)]


@pytest.mark.parametrize('parent,owned', [(1, frozenset({12})), (999, frozenset()), (1, frozenset())])
def test_live_or_unowned_holder_is_not_touched(monkeypatch, parent, owned):
    process = Process(12, '/saved/profile', ppid=parent)
    monkeypatch.setattr(browser_orphan.psutil, 'process_iter', lambda *_: [process])
    monkeypatch.setattr(browser_orphan.os, 'getpgid', lambda pid: 200 if not owned and parent == 1 else pid)
    terminate = Mock()
    monkeypatch.setattr(browser_orphan, '_terminate_group', terminate)
    with pytest.raises(browser_orphan.BrowserProfileInUse, match='Another browser is using the saved sign-in'):
        browser_orphan.close_orphans('/saved/profile', owned)
    terminate.assert_not_called()


def test_live_holder_prevents_sweep_of_other_matching_candidates(monkeypatch):
    processes = [Process(11, '/saved/profile'), Process(12, '/saved/profile', ppid=999)]
    monkeypatch.setattr(browser_orphan.psutil, 'process_iter', lambda *_: processes)
    monkeypatch.setattr(browser_orphan.os, 'getpgid', lambda pid: pid)
    terminate = Mock()
    monkeypatch.setattr(browser_orphan, '_terminate_group', terminate)
    with pytest.raises(browser_orphan.BrowserProfileInUse):
        browser_orphan.close_orphans('/saved/profile', frozenset())
    terminate.assert_not_called()


def test_unknown_startup_details_are_not_logged():
    assert browser_orphan.startup_message(RuntimeError('https://example.test cookie=secret')) == 'details withheld'


async def test_linux_launch_does_not_scan(monkeypatch):
    monkeypatch.setattr(browser_session_launch.sys, 'platform', 'linux')
    monkeypatch.setattr(browser_orphan, 'close_orphans', Mock(side_effect=AssertionError('scanned')))
    await browser_session_launch.start_chrome(SimpleNamespace(), SimpleNamespace())


def test_term_kill_and_confirmation(monkeypatch):
    signals = []
    monkeypatch.setattr(browser_orphan.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(browser_orphan, '_wait_for_group', Mock(side_effect=[False, True]))
    browser_orphan._terminate_group(11)
    assert signals == [(11, browser_orphan.signal.SIGTERM), (11, browser_orphan.signal.SIGKILL)]


@pytest.mark.parametrize('failure,message', [
    (RuntimeError('managed Chrome exited during startup'), 'managed browser startup failed; its processes were closed'),
    (browser_orphan.BrowserProfileInUse(), 'Another browser is using the saved sign-in'),
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
