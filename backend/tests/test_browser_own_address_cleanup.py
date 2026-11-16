"""Explicit native-browser job: launch drops the serving CORSIA's stored sign-in and nothing else."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from host_access import browser_cdp, browser_ports, browser_own_address as own
from host_access.browser_chrome import ChromeProcess
from tests.browser_own_address_fixture import ephemeral_pair, servers

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("CORSIA_NATIVE_BROWSER_TEST") != "1", reason="explicit native-browser job only")]


@contextlib.asynccontextmanager
async def _page(tmp_path, launch_number):
    """One launch of Chrome on the shared saved profile; closing it flushes the profile to disk."""
    executable = Path(os.environ["CORSIA_NATIVE_CHROME_BINARY"]).resolve(strict=True)
    chrome = await ChromeProcess.launch(str(executable), profile=str(tmp_path / "profile"), headless=True, ua=None,
                                        record=(tmp_path, f"session12345{launch_number}"))
    try:
        endpoint = await asyncio.to_thread(browser_cdp._page_endpoint, chrome.endpoint)
        async with browser_cdp.CdpClient(endpoint) as page:
            yield chrome, page
    finally:
        await chrome.close()


async def _evaluate(page, expression):
    result = await page.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
    assert "exceptionDetails" not in result, result["exceptionDetails"]
    return result["result"].get("value")


async def _visit(page, url, script):
    assert "errorText" not in await page.call("Page.navigate", url=url), url
    for _ in range(60):
        if await _evaluate(page, f"document.readyState==='complete'&&location.href.startsWith({url!r})"):
            return await _evaluate(page, script)
        await asyncio.sleep(.1)
    pytest.fail(f"{url} did not load")


async def test_launch_drops_the_serving_sign_in_and_keeps_every_other_origin_and_cookie(tmp_path, monkeypatch):
    """CORSIA keeps its sign-in in localStorage, not a cookie, so cleanup is per origin and spares localhost cookies."""
    monkeypatch.setattr(browser_ports, "reserve_pair", ephemeral_pair)
    async with servers() as fixture:
        monkeypatch.setattr(own, "_backend_ports", lambda: {fixture.serving})
        monkeypatch.setattr(own, "_frontend_ports", lambda ports: frozenset())
        read = "[localStorage.getItem('corsia_token'),document.cookie]"
        with monkeypatch.context() as unrestricted:  # the sign-in has to be made before the rules exist
            unrestricted.setattr(own, "resolver_rules", lambda: "")
            async with _page(tmp_path, 1) as (_, page):
                for host in ("127.0.0.1", "localhost"):
                    await _visit(page, f"http://{host}:{fixture.serving}/", "localStorage.setItem('corsia_token','secret')")
                await _visit(page, f"http://localhost:{fixture.other}/", "localStorage.setItem('corsia_token','other');"
                                                                        "document.cookie='unrelated=1; path=/'")
        async with _page(tmp_path, 2) as (chrome, _):
            await own.clear_serving_storage(chrome.endpoint)
        with monkeypatch.context() as unrestricted:  # read back through a launch that can still reach both
            unrestricted.setattr(own, "resolver_rules", lambda: "")
            async with _page(tmp_path, 3) as (_, page):
                gone = [await _visit(page, f"http://{host}:{fixture.serving}/", read) for host in ("127.0.0.1", "localhost")]
                kept = await _visit(page, f"http://localhost:{fixture.other}/", read)
    # The sign-in is gone from both names; the localhost cookie another page set is still there, on the serving port too.
    assert gone == [[None, ""], [None, "unrelated=1"]], gone
    assert kept == ["other", "unrelated=1"], kept
