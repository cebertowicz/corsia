"""Setup refusals are not cleanup state; ordinary command errors stay actionable."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from host_access import browser_cli as cli, browser_reporting, browser_sessions as sessions
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


async def _version(output):
    return sessions.CommandResult(0, output, '')


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['version', 'missing', 'launcher', 'mode'])
async def test_setup_refusal_names_cause_without_cleanup_state(synthetic_browser, monkeypatch, tmp_path, case):
    report = AsyncMock()
    monkeypatch.setattr(browser_reporting, 'report_cleanup', report)
    fake, args = synthetic_browser, ['snapshot']
    if case == 'version':
        original = fake.invoke
        async def invoke(argv, *, timeout):
            return await _version('agent-browser 0.28.1') if argv[-1] == '--version' else await original(argv, timeout=timeout)
        monkeypatch.setattr(fake.owner, '_invoke', invoke)
    elif case == 'missing':
        fake.owner.binary = str(tmp_path / 'absent' / 'agent-browser')
    elif case == 'launcher':
        launcher = tmp_path / 'agent-browser'
        launcher.write_text('#!/usr/bin/env node\n')
        launcher.chmod(0o755)
        fake.owner.binary = str(launcher)
    else:
        args = ['snapshot', '--provider', 'browserbase']
    with pytest.raises(sessions.BrowserSetupError) as caught:
        await fake.owner.run('thread', args)
    assert not isinstance(caught.value, sessions.BrowserSessionCleanupError)
    message = caught.value.tool_message
    expected = {'version': 'agent-browser 0.28.1 is not a supported native agent-browser version',
                'missing': 'not installed', 'launcher': 'not a script or launcher',
                'mode': 'verified local Chrome/CDP mode'}[case]
    assert expected in message and 'nothing needs cleanup' in message
    assert ('npm install -g agent-browser@0.31' in message) == (case != 'mode')
    assert 'operator investigation' not in message and 'blocked' not in message
    assert fake.owner.live_count() == 0 and not fake.calls
    report.assert_not_awaited()
    await fake.owner.shutdown()  # No residual cleanup responsibility.


@pytest.mark.asyncio
async def test_tool_surfaces_setup_cause(monkeypatch):
    from tools import browser, browser_runtime
    monkeypatch.setattr(browser_runtime, '_check_installed', lambda: None)
    failure = sessions.BrowserSetupError('agent-browser 0.28.1 is not a supported native agent-browser version')
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, 'run_browser', AsyncMock(side_effect=failure))
    result = await browser._run(['snapshot'], thread_id='thread')
    assert result == failure.tool_message and '0.28.1' in result


@pytest.mark.parametrize('body,available', [(None, True), ('#!/usr/bin/env node\n', False), ('', False)])
def test_capability_and_install_check_use_native_admission(monkeypatch, tmp_path, body, available):
    import sys
    from tools import browser_runtime
    target = tmp_path / 'agent-browser'
    if body is None:
        target.symlink_to(sys.executable)
    elif body:
        target.write_text(body)
        target.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    status = browser_runtime.browser_available()
    assert status['available'] is available
    assert (browser_runtime._check_installed() is None) is available
    if not available:
        assert 'npm install -g agent-browser@0.31' in status['install_hint']
        assert status['install_hint'] in browser_runtime._check_installed()


@pytest.mark.parametrize('stdout,stderr,expected', [
    (json.dumps({'success': False, 'error': 'Element not found: @e9 in private-session'}), 'ignored',
     'Error: agent-browser command failed: Element not found: @e9 in [browser-session]'),
    ('not json', 'net::ERR_NAME_NOT_RESOLVED at private-session',
     'Error: agent-browser command failed: net::ERR_NAME_NOT_RESOLVED at [browser-session]'),
    ('', '', 'Error: agent-browser command failed.'),
])
def test_command_error_detail_is_redacted_and_returned(stdout, stderr, expected):
    result = cli.CommandResult(1, stdout, stderr)
    assert cli.render_model_result(result, redactions={'private-session': '[browser-session]'}) == expected


@pytest.mark.asyncio
async def test_ordinary_command_error_keeps_session(synthetic_browser):
    fake = synthetic_browser
    fake.result = sessions.CommandResult(1, json.dumps({'success': False, 'error': 'Invalid selector'}), '')
    assert await fake.owner.run('thread', ['click', '#']) == 'Error: agent-browser command failed: Invalid selector'
    assert fake.owner.live_count() == 1
    fake.result = sessions.CommandResult(0, 'ok', '')
    assert await fake.owner.run('thread', ['snapshot']) == 'ok'
    assert await fake.owner.close_thread('thread') == 'Browser session closed.'
