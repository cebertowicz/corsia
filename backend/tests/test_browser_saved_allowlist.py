"""Saved-profile command shapes and fallback notices."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from host_access import browser_launch, browser_session_commands as commands, browser_session_launch as launch
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


@pytest.mark.parametrize('args', [
    ['open', 'about:blank'], ['snapshot'], ['snapshot', '-i'], ['click', '#a'],
    ['fill', '#a', 'value'], ['wait', '#a'], ['wait', '100'],
    ['wait', '--load', 'networkidle'], ['screenshot', '/tmp/a.png'],
    ['get', 'title'], ['get', 'url'], ['reload'],
])
def test_public_agent_shapes(args):
    assert commands._saved_command(args, pane=False)


def test_run_steps_reload_is_a_saved_session_shape():
    from tools import browser_run_steps
    command = browser_run_steps._command({'action': 'goto', 'reload': True})
    assert command == ['reload'] and commands._saved_command(command, pane=False)


@pytest.mark.parametrize('args', [
    ['open', 'https://example.com'], ['back'], ['forward'], ['reload'],
    ['get', 'title'], ['get', 'url'], ['set', 'viewport', '800', '600', '1'],
    ['set', 'media', 'dark'], ['set', 'media', 'light'],
])
def test_public_pane_shapes(args):
    assert commands._saved_command(args, pane=True)


@pytest.mark.parametrize('args', [
    ['network', 'route', '*', '--body', 'x'], ['set', 'headers', '{}'],
    ['set', 'credentials', 'u', 'p'], ['open', 'https://example.com', '--headers', '{}'],
    ['cookies'], ['get', 'cdp-url'], ['unknown'], ['plugins', 'add', 'x'],
    ['fill', '#a', '--headers'], ['fill', '#a', '-p'], ['fill', '#a', '-v'],
    ['fill', '#a', '-q'], ['eval', 'document.cookie'],
])
async def test_saved_public_path_refuses_other_shapes(synthetic_browser, monkeypatch, args):
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    with pytest.raises(Exception, match='unavailable in a browser session|not available to agents|verified local Chrome/CDP mode'):
        await synthetic_browser.owner.run('a', args)
    assert not synthetic_browser.calls
    await synthetic_browser.owner.shutdown()


def _pin_cli_0_31_2(synthetic_browser, monkeypatch):
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    synthetic_browser.code = synthetic_browser.code.replace("'0.27.0'", "'0.31.2'")
    original = synthetic_browser.owner._invoke
    async def invoke(argv, *, timeout):
        if argv[-1] == '--version':
            from host_access.browser_cli import CommandResult
            return CommandResult(0, 'agent-browser 0.31.2', '')
        return await original(argv, timeout=timeout)
    monkeypatch.setattr(synthetic_browser.owner, '_invoke', invoke)


@pytest.mark.parametrize('text', ['-1', '-example', 'javascript: documentation'])
async def test_saved_fill_preserves_ordinary_text(synthetic_browser, monkeypatch, text):
    _pin_cli_0_31_2(synthetic_browser, monkeypatch)
    await synthetic_browser.owner.run('a', ['open', 'about:blank'])
    await synthetic_browser.owner.run('a', ['fill', '#a', text])
    assert synthetic_browser.calls[-1][-3:] == ('fill', '#a', text)
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize('text', ['-p', '-pios', '-vq', '-qv', '-v1', '--headers'])
async def test_saved_fill_refuses_text_a_global_flag_could_read(synthetic_browser, monkeypatch, text):
    _pin_cli_0_31_2(synthetic_browser, monkeypatch)
    await synthetic_browser.owner.run('a', ['open', 'about:blank'])
    with pytest.raises(Exception, match='unavailable in a browser session|verified local Chrome/CDP mode'):
        await synthetic_browser.owner.run('a', ['fill', '#a', text])
    assert synthetic_browser.calls[-1][-2:] == ('open', 'about:blank')
    await synthetic_browser.owner.shutdown()


async def test_older_cli_keeps_conservative_dash_guard(synthetic_browser, monkeypatch):
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    await synthetic_browser.owner.run('a', ['open', 'about:blank'])
    await synthetic_browser.owner.run('a', ['fill', '#a', 'javascript: documentation'])
    with pytest.raises(Exception, match='unavailable in a browser session'):
        await synthetic_browser.owner.run('a', ['fill', '#a', '-1'])
    await synthetic_browser.owner.shutdown()


async def test_owner_database_failure_logs_and_notifies(synthetic_browser, monkeypatch, caplog):
    from infra import database
    def fail():
        raise sqlite3.OperationalError('fixture failure')
    monkeypatch.setattr(database, 'get_db', fail)
    result = await synthetic_browser.owner.run('a', ['open', 'about:blank'])
    assert "Saved sign-in wasn't used this time" in result
    assert 'owner lookup failed' in caplog.text
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize('holder', [None, SimpleNamespace(record=SimpleNamespace(binding=None, terminal_state='running'), command_lock=None)])
async def test_missing_or_stopped_holder_notifies(monkeypatch, holder, caplog):
    import asyncio
    from host_access.browser_session_state import BrowserRuntime, BrowserSessionRecord
    if holder is not None:
        holder.command_lock = asyncio.Lock()
    runtime = BrowserRuntime(BrowserSessionRecord('b', 'session', None, False, copy_of='a'))
    owner = SimpleNamespace(_sessions={} if holder is None else {'a': holder}, _invoke=AsyncMock())
    await launch.seed_copy(owner, runtime)
    assert "Saved sign-in wasn't used this time" in runtime.record.profile_notice
    assert 'browser sign-in copy unavailable' in caplog.text
    owner._invoke.assert_not_awaited()
