"""Regressions found in controlled-browser launch review."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from host_access import browser_cli, browser_launch, browser_session_launch
from host_access.browser_session_state import BrowserSessionCleanupError, BrowserSessionError
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


def test_native_client_discards_restore_environment_and_uses_private_empty_config(tmp_path):
    from host_access.browser_environment import client_environment, prepare_config
    config = prepare_config(tmp_path)
    assert config.read_text() == '{}' and config.stat().st_mode & 0o777 == 0o600
    source = {'HOME': '/fixture/home', 'AGENT_BROWSER_CONFIG': '/writable/config.json',
              'AGENT_BROWSER_SESSION_NAME': 'exported', 'AGENT_BROWSER_STATE': '/writable/state.json',
              'AGENT_BROWSER_RESTORE_SAVE': 'always', 'AGENT_BROWSER_PLUGINS': 'fixture'}
    env = client_environment(source, tmp_path, 39401)
    assert {key for key in env if key.startswith('AGENT_BROWSER_')} == {
        'AGENT_BROWSER_SOCKET_DIR', 'AGENT_BROWSER_STREAM_PORT', 'AGENT_BROWSER_DEFAULT_TIMEOUT'}
    assert env['AGENT_BROWSER_DEFAULT_TIMEOUT'] == '8000'
    assert env['HOME'] == source['HOME']


async def test_stream_is_verified_before_the_requested_first_action(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    verified = False
    original = fake.owner._invoke

    async def verify(*args, **kwargs):
        nonlocal verified
        assert not any('https://example.com' in call for call in fake.calls)
        verified = True

    async def invoke(argv, *, timeout):
        if 'https://example.com' in argv:
            assert verified, 'requested action preceded stream verification'
        return await original(argv, timeout=timeout)

    monkeypatch.setattr(browser_session_launch, 'verify_stream', verify)
    monkeypatch.setattr(fake.owner, '_invoke', invoke)
    await fake.owner.run('a', ('open', 'https://example.com'))
    endpoint = fake.owner._sessions['a'].chrome.endpoint
    assert fake.calls
    assert all(call[call.index('--cdp') + 1] == endpoint for call in fake.calls if '--cdp' in call)
    await fake.owner.shutdown()


async def test_stream_mismatch_prevents_the_first_requested_action(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    monkeypatch.setattr(browser_session_launch, 'verify_stream', AsyncMock(side_effect=RuntimeError('mismatch')))
    with pytest.raises(BrowserSessionError, match='startup failed'):
        await fake.owner.run('a', ('open', 'https://example.com'))
    assert not any('https://example.com' in call for call in fake.calls)
    assert fake.owner.live_count() == 0


async def test_linux_notice_is_once_for_agents_and_never_changes_pane_json(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    monkeypatch.setattr(browser_session_launch.sys, 'platform', 'linux')
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    fake.result = browser_cli.CommandResult(0, json.dumps({'success': True, 'data': {'url': 'about:blank'}}), '')
    pane = await fake.owner.run('linux-notice-owner', ('get', 'url'), pane=True)
    assert json.loads(pane) == {'url': 'about:blank'}
    assert "Saved sign-in isn't available on Linux" in await fake.owner.run('linux-notice-owner', ('snapshot',))
    assert "Saved sign-in isn't available on Linux" not in await fake.owner.run('linux-notice-owner', ('snapshot',))
    await fake.owner.shutdown()


async def test_close_race_resolves_saved_profile_for_replacement(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner')
    await fake.owner._admit('a', lease_id=None)
    await fake.owner._owner_lock.acquire()
    admission = asyncio.create_task(fake.owner._admit('a', lease_id=None))
    try:
        await asyncio.sleep(0)
        fake.owner._sessions.pop('a')  # Close removes the previous runtime while admission waits.
    finally:
        fake.owner._owner_lock.release()
    replacement = await admission
    assert replacement.record.profile == str(browser_launch.profile_dir('a'))
    await fake.owner.shutdown()


async def test_secondary_chrome_cleanup_failure_keeps_original_cause_and_handle(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('a', ('open', 'about:blank'))
    runtime = fake.owner._sessions['a']
    chrome = runtime.chrome
    chrome.close.side_effect = RuntimeError('chrome cleanup failed')
    original = BrowserSessionCleanupError('original daemon cleanup failure')
    monkeypatch.setattr(fake.owner, '_observe', AsyncMock(side_effect=original))
    with pytest.raises(BrowserSessionCleanupError) as raised:
        await fake.owner.close_thread('a')
    assert raised.value.__cause__ is original
    assert runtime.chrome is chrome
    assert runtime.record.terminal_state == 'death_unverified'
