"""macOS managed clients work on restricted ports while page routes are refused."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest
import websockets

from host_access import browser_ports, browser_session_launch
from host_access.browser_sessions import BrowserSessionOwner
from tests.browser_restricted_ports_fixture import assert_restricted_routes
from tests.browser_restricted_ports_fixture import restricted_listeners  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get('CORSIA_NATIVE_BROWSER_TEST') != '1',
                       reason='explicit native-browser job only'),
    pytest.mark.skipif(sys.platform != 'darwin', reason='managed Chrome is macOS-only'),
]


@pytest.fixture
async def restricted_fixture(monkeypatch, restricted_listeners):
    """One ephemeral managed session; no signed-in profile or second session."""
    _, allowed, _ = restricted_listeners
    monkeypatch.setattr(browser_session_launch, 'resolve_profile', AsyncMock(return_value=(None, None)))
    owner = BrowserSessionOwner(namespace='corsia-restricted-proof', instance_key='disposable-listeners')
    try:
        result = await owner.run('proof', ('open', f'http://127.0.0.1:{allowed}/start'))
        assert not result.startswith('Error'), result
        yield owner, restricted_listeners
    finally:
        await owner.shutdown()
        assert owner.live_count() == 0


async def test_restricted_ports_refuse_page_routes_and_allow_native_clients(restricted_fixture):
    """Retain native open/CDP/stream proof alongside the shared page-route checks."""
    owner, listeners = restricted_fixture
    chrome = owner._sessions['proof'].chrome
    assert chrome.cdp_port in browser_ports.RESERVED_PORTS
    assert chrome.stream_port in browser_ports.RESERVED_PORTS
    async with websockets.connect(f'ws://127.0.0.1:{chrome.stream_port}', open_timeout=3) as stream:
        async with asyncio.timeout(10):
            while True:
                message = json.loads(await stream.recv())
                if message.get('type') == 'frame':
                    assert message.get('data')
                    break
    results = await assert_restricted_routes(chrome.endpoint, listeners, owner=owner)
    results['managed_ports'] = [chrome.cdp_port, chrome.stream_port]
    results['native_stream_frame'] = True
    print(json.dumps(results), flush=True)
