"""Executable provenance must be established before managed browser code executes."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from host_access import browser_executables as trusted, browser_identity as identity, browser_launch


def test_native_copy_is_authenticated_and_does_not_follow_mutable_source(tmp_path, monkeypatch):
    source = tmp_path / 'native'
    content = b'fixture release bytes'
    source.write_bytes(content)
    monkeypatch.setattr(trusted, 'NATIVE_SHA256', {(trusted.platform.system(), trusted.platform.machine()): hashlib.sha256(content).hexdigest()})
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    copied = Path(trusted.private_native(str(source)))
    assert copied != source and copied.read_bytes() == content
    assert copied.stat().st_mode & 0o777 == 0o700
    source.write_bytes(b'replacement')
    assert copied.read_bytes() == content
    with pytest.raises(identity.BrowserIdentityError, match='pinned native'):
        trusted.private_native(str(source))


def test_native_checksum_mismatch_never_creates_a_runnable_copy(tmp_path, monkeypatch):
    source = tmp_path / 'untrusted'
    source.write_bytes(b'not a release')
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    with pytest.raises(identity.BrowserIdentityError, match='pinned native'):
        trusted.private_native(str(source))
    assert not (tmp_path / 'private').exists()


def test_corrupted_private_native_is_repaired_from_authenticated_source(tmp_path, monkeypatch):
    source = tmp_path / 'native'
    source.write_bytes(b'release')
    monkeypatch.setattr(trusted, 'NATIVE_SHA256', {(trusted.platform.system(), trusted.platform.machine()): hashlib.sha256(b'release').hexdigest()})
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    copied = Path(trusted.private_native(str(source)))
    copied.write_bytes(b'changed')
    assert trusted.private_native(str(source)) == str(copied)
    assert copied.read_bytes() == b'release'


def test_google_verification_requires_vendor_identity_before_any_browser_probe(tmp_path, monkeypatch):
    executable = tmp_path / 'Chrome.app' / 'Contents' / 'MacOS' / 'Chrome'
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(trusted.platform, 'system', lambda: 'Darwin')
    run = Mock(return_value=SimpleNamespace(returncode=1))
    monkeypatch.setattr(trusted.subprocess, 'run', run)
    with pytest.raises(identity.BrowserIdentityError, match='signature'):
        trusted.verify_google(str(executable))
    assert run.call_args.args[0] == ['/usr/bin/codesign', '--verify', '--deep',
                                    '-R=' + trusted.GOOGLE_REQUIREMENT, str(tmp_path / 'Chrome.app')]


def test_signed_app_from_writable_location_is_copied_before_launch(tmp_path, monkeypatch):
    import shutil
    executable = tmp_path / 'user-apps' / 'Chrome.app' / 'Contents' / 'MacOS' / 'Chrome'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'signed fixture')
    monkeypatch.setattr(trusted.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    verify = Mock()
    monkeypatch.setattr(trusted, 'verify_google', verify)

    def ditto(argv, **kwargs):
        assert argv[:2] == ['/usr/bin/ditto', '--norsrc']
        shutil.copytree(argv[2], argv[3])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(trusted.subprocess, 'run', ditto)
    copied = Path(trusted.trusted_chrome(str(executable)))
    assert copied.is_relative_to(trusted.runtime_root()) and copied.read_bytes() == executable.read_bytes()
    assert verify.call_args_list[0].args == (str(executable),)
    staged = Path(verify.call_args_list[1].args[0])
    assert staged.is_relative_to(trusted.runtime_root()) and staged != executable
    assert staged.parts[-4:] == copied.parts[-4:]


async def test_failed_native_authentication_never_executes_version_probe(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    calls = Mock(side_effect=identity.BrowserIdentityError('authentication failed'))
    monkeypatch.setattr(trusted, 'private_native', calls)
    invoked = Mock(side_effect=AssertionError('browser code executed before authentication'))
    monkeypatch.setattr(fake.owner, '_invoke', invoked)
    from host_access.browser_session_state import BrowserSetupError
    with pytest.raises(BrowserSetupError, match='authentication failed'):
        await fake.owner.run('thread', ('snapshot',))
    invoked.assert_not_called()
    assert fake.owner.live_count() == 0


async def test_private_download_uses_fresh_private_home_and_reuses_only_completed_install(tmp_path, monkeypatch):
    from host_access import browser_cli, browser_download
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    homes = []

    async def install(argv, *, socket_dir, config_path, home, timeout):
        assert argv == ('verified-native', 'install')
        assert home.is_relative_to(trusted.runtime_root())
        assert not list(home.iterdir())
        homes.append(home)
        executable = home / '.agent-browser/browsers/chrome-156/Chrome.app/Contents/MacOS/Google Chrome for Testing'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fresh download fixture')
        return browser_cli.CommandResult(0, '', '')

    monkeypatch.setattr(browser_cli, 'invoke', install)
    first = await browser_download.install('verified-native', tmp_path)
    assert browser_download.is_private_download(first)
    assert await browser_download.install('verified-native', tmp_path) == first
    assert len(homes) == 1


async def test_failed_private_download_is_never_marked_as_trusted(tmp_path, monkeypatch):
    from host_access import browser_cli, browser_download
    from unittest.mock import AsyncMock
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    monkeypatch.setattr(browser_cli, 'invoke', AsyncMock(return_value=browser_cli.CommandResult(1, '', 'fixture failure')))
    with pytest.raises(identity.BrowserIdentityError, match='Shared browser needs Google Chrome'):
        await browser_download.install('verified-native', tmp_path)
    assert browser_download.cached_chrome() is None


@pytest.fixture
def packaged_native(tmp_path, monkeypatch):
    import plistlib
    bundle = tmp_path / 'Fork.app'
    contents = bundle / 'Contents'
    source = contents / 'Resources/bin/agent-browser'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'resigned native fixture')
    (contents / 'MacOS').mkdir()
    main = contents / 'MacOS/Fork'
    main.write_bytes(b'main fixture')
    (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Fork'}))
    monkeypatch.setattr(trusted, 'app_bundle_root', lambda: bundle)
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path / 'private')
    return source, main


def test_packaged_native_verifies_private_copy_against_running_app_team(packaged_native, monkeypatch):
    source, main = packaged_native
    calls = []

    def codesign(argv, **kwargs):
        calls.append(argv)
        if '--display' in argv:
            assert argv == ['/usr/bin/codesign', '--display', '--verbose=4', str(main)]
            return SimpleNamespace(returncode=0, stderr='TeamIdentifier=FORK123456\n')
        copied = Path(argv[-1])
        assert copied != source and copied.is_relative_to(trusted.runtime_root())
        assert copied.read_bytes() == b'resigned native fixture'
        assert copied.stat().st_mode & 0o777 == 0o700
        assert argv == ['/usr/bin/codesign', '--verify', '--strict',
                        '-R=anchor apple generic and certificate leaf[subject.OU] = "FORK123456"', str(copied)]
        source.write_bytes(b'replaced after copying')
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(trusted.subprocess, 'run', codesign)
    copied = Path(trusted.private_native(str(source)))
    assert copied == Path(calls[-1][-1]) and copied.read_bytes() == b'resigned native fixture'
    assert len(calls) == 2


@pytest.mark.parametrize('signature', ['Signature=adhoc\nTeamIdentifier=not set\n', '', 'TeamIdentifier=not set\n'])
def test_unsigned_running_app_refuses_before_copy(packaged_native, monkeypatch, signature):
    source, _ = packaged_native
    runner = Mock(return_value=SimpleNamespace(returncode=0, stderr=signature))
    monkeypatch.setattr(trusted.subprocess, 'run', runner)
    with pytest.raises(identity.BrowserIdentityError, match='unsigned app build'):
        trusted.private_native(str(source))
    assert runner.call_count == 1
    assert not (source.parents[4] / 'private').exists()


@pytest.mark.parametrize('copy_signature', ['adhoc', 'other-team', 'timeout'])
def test_packaged_native_rejects_untrusted_copy_and_deletes_it(packaged_native, monkeypatch, copy_signature):
    import subprocess
    source, _ = packaged_native
    copies = []

    def codesign(argv, **kwargs):
        if '--display' in argv:
            return SimpleNamespace(returncode=0, stderr='TeamIdentifier=FORK123456\n')
        copied = Path(argv[-1])
        copies.append(copied)
        assert copied.read_bytes() == source.read_bytes()
        assert 'FORK123456' in argv[-2]
        if copy_signature == 'timeout':
            raise subprocess.TimeoutExpired(argv, 30)
        return SimpleNamespace(returncode=1, stderr=f'{copy_signature}: requirement failed')

    monkeypatch.setattr(trusted.subprocess, 'run', codesign)
    with pytest.raises(identity.BrowserIdentityError, match='signature could not be verified'):
        trusted.private_native(str(source))
    assert len(copies) == 1 and not copies[0].exists()
    assert source.exists()


def test_packaged_native_reuses_authenticated_copy_without_accumulation(packaged_native, monkeypatch):
    source, _ = packaged_native
    def codesign(argv, **kwargs):
        return SimpleNamespace(returncode=0, stderr='TeamIdentifier=FORK123456\n')
    monkeypatch.setattr(trusted.subprocess, 'run', codesign)
    first = trusted.private_native(str(source))
    for _ in range(3):
        assert trusted.private_native(str(source)) == first
    assert list(trusted.runtime_root().iterdir()) == [Path(first)]


from tests.browser_process_fixture import synthetic_browser  # noqa: E402, F401
