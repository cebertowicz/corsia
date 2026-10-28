"""Linux browser selection and setup refusal."""
import json
import sys
from types import SimpleNamespace

import pytest
from host_access import browser_cli as cli, browser_identity as identity, browser_launch
from host_access import browser_session_launch as launch, browser_sessions as sessions
from host_access.browser_cli import chrome_executable as select_chrome
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


@pytest.mark.asyncio
async def test_linux_spawn_uses_managed_path_and_removes_inherited_args(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setenv('AGENT_BROWSER_EXECUTABLE_PATH', '/unsafe/browser')
    monkeypatch.setenv('AGENT_BROWSER_ARGS', '--no-sandbox')
    monkeypatch.setenv('CI', 'true')
    code = "import json, os; print(json.dumps([os.getenv('AGENT_BROWSER_EXECUTABLE_PATH'), os.getenv('AGENT_BROWSER_ARGS'), os.getenv('CI')]))"
    result = await cli.invoke((sys.executable, '-c', code), socket_dir=tmp_path, timeout=10,
                              linux_chrome='/opt/corsia/chromium/chrome')
    assert result.returncode == 0
    assert json.loads(result.stdout) == ['/opt/corsia/chromium/chrome', None, None]


@pytest.mark.parametrize('blocked', ['--no-sandbox', '--no-sandbox=true',
                                     '--disable-setuid-sandbox', '--disable-setuid-sandbox=true'])
def test_linux_launch_keeps_other_inherited_flags(monkeypatch, blocked):
    monkeypatch.setattr(browser_launch, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(launch, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setenv('AGENT_BROWSER_ARGS', f'{blocked} --foo=1')
    args = launch.flags(SimpleNamespace(show_window=False), None)
    assert args[0] == '--args'
    assert '--foo=1' in args[1]
    assert blocked not in args[1]
    assert '--disable-blink-features=AutomationControlled' in args[1]


@pytest.mark.parametrize(('show_window', 'ua'), [(False, 'Mozilla/5.0 (X11; Linux x86_64) Chrome/156.0.0.0'),
                                                    (False, None), (False, ''), (True, 'Fixture UA')])
def test_linux_launch_user_agent_is_a_separate_argument(monkeypatch, show_window, ua):
    monkeypatch.setattr(browser_launch, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(launch, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setenv('AGENT_BROWSER_ARGS', '--foo=1,--bar=2\n--baz=3')
    args = launch.flags(SimpleNamespace(show_window=show_window), ua)
    assert args[0:2] == ['--args', '--foo=1\n--bar=2\n--baz=3\n--disable-blink-features=AutomationControlled']
    assert ' ' not in args[1]
    if not show_window and ua is not None:
        assert args[2:4] == ['--user-agent', ua]
        assert args.count(ua) == 1
        assert args[4:] == ['--init-script', str(browser_launch.STEALTH_INIT_SCRIPT)]
    else:
        assert '--user-agent' not in args
        assert args[2:] == ['--init-script', str(browser_launch.STEALTH_INIT_SCRIPT)]


@pytest.mark.asyncio
async def test_linux_never_downloads_on_first_use(synthetic_browser, monkeypatch):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(launch, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(cli, 'chrome_executable', lambda: ('/opt/corsia/chromium/chrome', 'system'))
    monkeypatch.setattr(cli, 'chrome_download_missing', lambda: pytest.fail('Linux tried to download'))
    await synthetic_browser.owner._pin_native()
    assert synthetic_browser.owner._native is not None
    await synthetic_browser.owner.shutdown()


def test_linux_selects_managed_browser_and_ignores_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    managed = tmp_path / 'managed' / 'chrome'
    system = tmp_path / 'system' / 'chrome'
    managed.parent.mkdir()
    managed.write_text('browser')
    managed.chmod(0o755)
    monkeypatch.setattr(cli, 'LINUX_CHROME_PATHS', (system, managed))
    monkeypatch.setenv('AGENT_BROWSER_EXECUTABLE_PATH', '/unsafe/browser')
    assert cli.chrome_executable() == (str(managed), 'system')
    system.parent.mkdir()
    system.write_text('browser')
    system.chmod(0o755)
    assert cli.chrome_executable() == (str(system), 'system')


@pytest.mark.asyncio
async def test_missing_linux_browser_refuses_without_a_download(synthetic_browser, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(launch, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(cli, 'chrome_executable', select_chrome)
    monkeypatch.setattr(cli, 'LINUX_CHROME_PATHS', (tmp_path / 'missing',))
    monkeypatch.setattr(cli, 'chrome_download_missing', lambda: pytest.fail('Linux tried to download'))
    with pytest.raises(sessions.BrowserSetupError, match='Re-run the CORSIA installer'):
        await synthetic_browser.owner.run('thread', ['snapshot'])
    assert not synthetic_browser.calls
    assert synthetic_browser.owner.live_count() == 0
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize('argument', ['--args=--no-sandbox', '--executable-path=/unsafe/chrome'])
def test_linux_refuses_browser_launch_overrides(monkeypatch, argument):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    with pytest.raises(identity.BrowserIdentityError, match='Remove browser launch arguments'):
        cli.check_local_mode([argument, 'open', 'about:blank'])


@pytest.mark.parametrize('config_value', [{'args': ['--no-sandbox']}, {'executablePath': '/unsafe/chrome'}])
def test_linux_refuses_browser_config_that_can_disable_the_sandbox(monkeypatch, tmp_path, config_value):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(config_value))
    monkeypatch.setenv('AGENT_BROWSER_CONFIG', str(config))
    with pytest.raises(identity.BrowserIdentityError, match='Remove browser launch arguments'):
        cli.check_local_mode(['open', 'about:blank'])


def test_linux_missing_browser_names_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(cli, 'LINUX_CHROME_PATHS', (tmp_path / 'missing',))
    with pytest.raises(identity.BrowserIdentityError, match='Re-run the CORSIA installer'):
        cli.chrome_executable()
