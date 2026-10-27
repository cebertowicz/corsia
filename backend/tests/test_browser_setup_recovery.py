"""Failures before browser admission remain recoverable setup failures."""
import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from host_access import browser_executables as trusted, browser_identity as identity, browser_launch


def test_partial_native_is_atomically_repaired_from_pinned_bytes(tmp_path, monkeypatch):
    source = tmp_path/'native'
    source.write_bytes(b'fixture release')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(trusted, 'NATIVE_SHA256', {(trusted.platform.system(), trusted.platform.machine()): digest})
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path/'private')
    target = trusted.runtime_root()/f'agent-browser-{digest}'
    target.write_bytes(b'partial')
    assert Path(trusted.private_native(str(source))).read_bytes() == source.read_bytes()
    assert not list(trusted.runtime_root().glob('agent-browser-copy-*'))


@pytest.mark.parametrize('failure', [OSError('fixture OS error'), subprocess.TimeoutExpired('codesign', 30)])
def test_google_probe_errors_remain_setup_failures(tmp_path, monkeypatch, failure):
    executable = tmp_path/'Chrome.app/Contents/MacOS/Chrome'
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(trusted.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(trusted.subprocess, 'run', Mock(side_effect=failure))
    with pytest.raises(identity.BrowserIdentityError, match='signature'):
        trusted.verify_google(str(executable))


@pytest.mark.parametrize('fail_copy', [False, True])
def test_partial_google_copy_is_repaired_or_removed(tmp_path, monkeypatch, fail_copy):
    source = tmp_path/'source/Chrome.app/Contents/MacOS/Chrome'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'signed complete fixture')
    monkeypatch.setattr(trusted.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path/'private')
    target = trusted.runtime_root()/'Chrome.app'
    target.mkdir()
    (target/'partial').touch()

    def verify(path):
        if not Path(path).is_file():
            raise identity.BrowserIdentityError('partial fixture')

    def copy(argv, **kwargs):
        shutil.copytree(argv[2], argv[3])
        return SimpleNamespace(returncode=int(fail_copy))

    monkeypatch.setattr(trusted, 'verify_google', verify)
    monkeypatch.setattr(trusted.subprocess, 'run', copy)
    if fail_copy:
        with pytest.raises(identity.BrowserIdentityError):
            trusted.trusted_chrome(str(source))
        assert not target.exists()
    else:
        assert Path(trusted.trusted_chrome(str(source))).read_bytes() == source.read_bytes()
    assert not list(trusted.runtime_root().glob('chrome-copy-*'))


@pytest.mark.parametrize('marker', ['missing', 'malformed', 'missing-version', 'old-version', 'missing-executable', 'legacy'])
async def test_download_repairs_marker_and_pin_mismatch(tmp_path, monkeypatch, marker):
    import json
    from host_access import browser_cli, browser_download
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path/'private')
    root = trusted.runtime_root()
    old = root/('chrome-home-legacy/old' if marker == 'legacy' else 'chrome-installed/old')
    old.parent.mkdir()
    old.write_bytes(b'old')
    metadata = {'executable': str(old.relative_to(root)), 'agent_browser_version': trusted.PINNED_NATIVE_VERSION}
    if marker == 'old-version':
        metadata['agent_browser_version'] = 'old-pin'
    elif marker in {'missing-version', 'legacy'}:
        metadata.pop('agent_browser_version')
    elif marker == 'missing-executable':
        metadata['executable'] = 'chrome-installed/absent'
    if marker != 'missing':
        (root/'chrome-download.json').write_text('invalid' if marker == 'malformed' else json.dumps(metadata))
    homes = []

    async def install(argv, *, home, **kwargs):
        assert not old.exists()
        homes.append(home)
        executable = home/'.agent-browser/browsers/chrome-156/Chrome.app/Contents/MacOS/Google Chrome for Testing'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'new fixture')
        return browser_cli.CommandResult(0, '', '')

    monkeypatch.setattr(browser_cli, 'invoke', install)
    path = Path(await browser_download.install('verified-native', tmp_path))
    assert path.read_bytes() == b'new fixture'
    assert browser_download.cached_chrome() == str(path)
    assert json.loads((root/'chrome-download.json').read_text())['agent_browser_version'] == trusted.PINNED_NATIVE_VERSION
    assert len(homes) == 1 and not homes[0].exists()


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'os-error', 'cancel'])
async def test_failed_download_removes_partial_folder(tmp_path, monkeypatch, failure):
    from host_access import browser_cli, browser_download
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path/'private')
    homes = []

    async def install(argv, *, home, **kwargs):
        homes.append(home)
        (home/'partial').write_bytes(b'partial archive')
        if failure == 'os-error':
            raise OSError('fixture failure')
        if failure == 'cancel':
            raise browser_cli.CommandCancelled(browser_cli.CommandResult(-1, '', '', completion='interrupted'))
        return browser_cli.CommandResult(1, '', '', timed_out=failure == 'timeout')

    monkeypatch.setattr(browser_cli, 'invoke', install)
    exception = browser_cli.CommandCancelled if failure == 'cancel' else identity.BrowserIdentityError
    with pytest.raises(exception):
        await browser_download.install('verified-native', tmp_path)
    assert len(homes) == 1 and not homes[0].exists()
    assert not (trusted.runtime_root()/'chrome-download.json').exists()


async def test_late_setup_failure_does_not_pin_incomplete_owner(synthetic_browser, monkeypatch):
    from host_access import browser_session_launch
    from host_access.browser_session_state import BrowserSetupError
    fake = synthetic_browser
    with monkeypatch.context() as failing:
        failing.setattr(browser_session_launch, 'prepare_directories', Mock(side_effect=OSError('fixture disk failure')))
        with pytest.raises(BrowserSetupError, match='try browser setup again'):
            await fake.owner._pin_native()
    assert fake.owner._native is None and fake.owner.live_count() == 0
    await fake.owner._pin_native()
    assert fake.owner._native is not None
    await fake.owner.shutdown()


async def test_pending_installer_keeps_home_until_client_settles(tmp_path, monkeypatch):
    import asyncio
    from host_access import browser_cli, browser_download
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path/'private')
    release = asyncio.Event()
    homes = []
    async def settle():
        await release.wait()
        return True
    pending = asyncio.create_task(settle())
    async def install(argv, *, home, **kwargs):
        homes.append(home)
        return browser_cli.CommandResult(-1, '', '', True, 'unknown', pending)
    monkeypatch.setattr(browser_cli, 'invoke', install)
    with pytest.raises(identity.BrowserIdentityError):
        await browser_download.install('verified-native', tmp_path)
    assert homes[0].exists()
    release.set()
    await pending
    await asyncio.sleep(0)  # Deliver the retained task's cleanup callback.
    assert not homes[0].exists()


async def test_marker_write_failure_removes_completed_attempt(tmp_path, monkeypatch):
    from host_access import browser_cli, browser_download
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path/'private')
    async def install(argv, *, home, **kwargs):
        executable = home/'.agent-browser/browsers/chrome-156/Chrome.app/Contents/MacOS/Google Chrome for Testing'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture')
        return browser_cli.CommandResult(0, '', '')
    monkeypatch.setattr(browser_cli, 'invoke', install)
    original = Path.write_text
    def write(path, *args, **kwargs):
        if path.name == 'chrome-download.json':
            raise OSError('fixture disk failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', write)
    with pytest.raises(identity.BrowserIdentityError):
        await browser_download.install('verified-native', tmp_path)
    assert not list(trusted.runtime_root().glob('chrome-*'))


from tests.browser_process_fixture import synthetic_browser  # noqa: E402, F401
