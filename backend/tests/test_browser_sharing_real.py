"""Host-only proof that sharing preserves a live browser."""
import os
from uuid import uuid4
from types import SimpleNamespace

import psutil
import pytest

from host_access import browser_cdp, browser_control, browser_pane
from host_access.browser_sessions import BrowserSessionOwner
from tests.test_browser_shared_e2e import fixture_server


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_headless_browser_shares_without_relaunch(monkeypatch):
    if os.environ.get('CORSIA_BROWSER_E2E') != '1':
        pytest.skip('requires isolated host browser environment')
    thread = f'share-real-{uuid4().hex}'
    owner = BrowserSessionOwner(namespace='corsia-share-real', instance_key=uuid4().hex)
    notices = []
    monkeypatch.setattr(browser_control, 'announce_browser_opened', notices.append)
    try:
        with fixture_server() as origin:
            url = f'{origin}/plain.html'
            await owner.run(thread, ('open', url), session='new')
            runtime = owner._sessions[thread]
            pid = runtime.chrome.process.pid
            assert any(flag.startswith('--headless') for flag in psutil.Process(pid).cmdline())
            assert notices == []
            endpoint = await owner.page_endpoint(thread)
            async with browser_cdp.CdpClient(endpoint) as cdp:
                await cdp.call('Runtime.evaluate', expression="document.cookie = 'shareproof=present; path=/'")
            await owner.run(thread, ('open', url), session='existing', show_window=True)
            assert runtime.chrome.process.pid == pid
            assert runtime.record.shared and not runtime.record.show_window and notices == [thread]
            endpoint = await owner.page_endpoint(thread)
            async with browser_cdp.CdpClient(endpoint) as cdp:
                result = await cdp.call('Runtime.evaluate', expression='document.cookie', returnByValue=True)
            assert 'shareproof=present' in result['result']['value']
    finally:
        await owner.close_thread(thread)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_first_shared_open_is_headless(monkeypatch):
    if os.environ.get('CORSIA_BROWSER_E2E') != '1':
        pytest.skip('requires isolated host browser environment')
    thread = f'shared-first-{uuid4().hex}'
    owner = BrowserSessionOwner(namespace='corsia-share-real', instance_key=uuid4().hex)
    monkeypatch.setattr(browser_control, 'announce_browser_opened', lambda _thread: None)
    monkeypatch.setattr(browser_pane, 'PROCESS_OWNER', SimpleNamespace(browser_sessions=lambda: owner))
    try:
        with fixture_server() as origin:
            await browser_pane.open_url(thread, f'{origin}/plain.html')
            assert owner._sessions[thread].record.shared and not owner._sessions[thread].record.show_window
            pid = owner._sessions[thread].chrome.process.pid
            assert any(flag.startswith('--headless') for flag in psutil.Process(pid).cmdline())
    finally:
        await owner.close_thread(thread)
