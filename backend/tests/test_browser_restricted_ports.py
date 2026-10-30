"""Explicit native-browser job: real Chrome restricted-port refusal on each platform."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket

import pytest

from host_access import browser_ports
from host_access.browser_chrome import ChromeProcess
from tests.browser_restricted_ports_fixture import assert_restricted_routes, listener_pair

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('CORSIA_NATIVE_BROWSER_TEST') != '1', reason='explicit native-browser job only',
)]


def _reserve_test_controls():
    """Keep the test's own control sockets outside the inventory being measured."""
    held = []
    try:
        for _ in range(2):
            listener = socket.socket()
            held.append(listener)
            listener.bind(('127.0.0.1', 0))
            assert listener.getsockname()[1] not in browser_ports.RESERVED_PORTS
        return tuple(held)
    except BaseException:
        for listener in held:
            listener.close()
        raise


async def test_chrome_restricted_ports_refuse_all_page_routes(tmp_path, monkeypatch):
    """Refuse every tuple entry in one real browser, with unrestricted controls per entry."""
    executable = Path(os.environ['CORSIA_NATIVE_CHROME_BINARY']).resolve(strict=True)
    assert os.geteuid() != 0, 'native browser regression must run as an ordinary user'
    # Only the test's control-port allocation changes; real Chrome/network behavior
    # and the complete production inventory under test remain unchanged.
    with monkeypatch.context() as setup:
        setup.setattr(browser_ports, 'reserve_pair', _reserve_test_controls)
        chrome = await ChromeProcess.launch(str(executable), profile=str(tmp_path / 'chrome-profile'),
                                            headless=True, ua=None)
    tested = []
    try:
        for port in browser_ports.RESERVED_PORTS:
            async with listener_pair((port,)) as listeners:
                results = await assert_restricted_routes(chrome.endpoint, listeners)
                results['restricted_port'] = port
                print(json.dumps(results), flush=True)
                tested.append(port)
        assert tuple(tested) == browser_ports.RESERVED_PORTS
        print(json.dumps({'tested_ports': tested, 'port_count': len(tested),
                          'single_browser': True}), flush=True)
    finally:
        await chrome.close()
        assert chrome.closed
