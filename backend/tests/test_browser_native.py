"""Explicit real-native job: no provider calls and no browser sandbox bypass.

Select with CORSIA_NATIVE_BROWSER_TEST=1 and absolute binary/Chrome paths.
Generic integration suites do not install or launch this optional native dependency.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil

import psutil
import pytest
from host_access import browser_cdp, browser_pane, browser_screencast
from host_access import browser_identity as identity
from host_access.browser_sessions import BrowserSessionError, BrowserSessionOwner
from ws import browser_clipboard

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('CORSIA_NATIVE_BROWSER_TEST') != '1', reason='explicit native-browser job only',
)]


@pytest.fixture
async def native_owner(monkeypatch, tmp_path):
    binary = Path(os.environ['CORSIA_NATIVE_BROWSER_BINARY']).resolve(strict=True)
    chrome = Path(os.environ['CORSIA_NATIVE_CHROME_BINARY']).resolve(strict=True)
    assert os.geteuid() != 0, 'native browser smoke must run as an ordinary user'
    for key in tuple(os.environ):
        if key.startswith('AGENT_BROWSER_'):
            monkeypatch.delenv(key)
    # Stock native adds --no-sandbox merely when CI exists. Removing this
    # process-local hint preserves Chrome's sandbox; never weaken host policy.
    monkeypatch.delenv('CI', raising=False)
    config = tmp_path / 'agent-browser.json'
    config.write_text('{}')
    monkeypatch.setenv('AGENT_BROWSER_CONFIG', str(config))
    monkeypatch.setenv('AGENT_BROWSER_EXECUTABLE_PATH', str(chrome))
    monkeypatch.setenv('AGENT_BROWSER_PROFILE', str(tmp_path / 'chrome-profile'))
    owner = BrowserSessionOwner(namespace='corsia-test', binary=str(binary))
    assert owner.socket_dir.parent == identity.default_socket_root()
    try:
        yield owner
    finally:
        await owner.shutdown()
        assert owner.live_count() == 0
        if owner.socket_dir.exists():
            shutil.rmtree(owner.socket_dir)


async def test_display_secret_and_sign_in_are_distinguished_in_chrome(native_owner):
    thread_id = 'native-display-secret'
    assert not (await native_owner.run(thread_id, ['open', 'about:blank'])).startswith('Error:')
    endpoint = await native_owner.page_endpoint(thread_id)
    async with browser_cdp.CdpClient(endpoint) as cdp:
        await cdp.call('Runtime.evaluate', expression="""document.body.innerHTML = `
          <div><input type="password" value="fixture" readonly></div>
          <div><div><input type="password" value="fixture"></div><div><button>Show</button><button>Regenerate</button></div></div>`""")
    assert not (await browser_cdp.page_password_probe(endpoint, {})).visible_nodes
    async with browser_cdp.CdpClient(endpoint) as cdp:
        await cdp.call('Runtime.evaluate', expression="""document.body.innerHTML = `
          <form><input type="email"><div><input type="password" value="fixture" readonly>
          <button type="button" aria-label="Show password">Show</button></div><button>Sign in</button></form>`""")
    assert len((await browser_cdp.page_password_probe(endpoint, {})).visible_nodes) == 1
    async with browser_cdp.CdpClient(endpoint) as cdp:
        await cdp.call('Runtime.evaluate', expression="""document.body.innerHTML = `
          <div><input type="password" value="fixture"><button>Show</button></div>`""")
    assert not (await browser_cdp.page_password_probe(endpoint, {})).visible_nodes
    async with browser_cdp.CdpClient(endpoint) as cdp:
        await cdp.call('Runtime.evaluate', expression="""document.body.innerHTML = `
          <div><input type="password"><button>Show password</button><div role="button">Next</div></div>`""")
    assert len((await browser_cdp.page_password_probe(endpoint, {})).visible_nodes) == 1
    async with browser_cdp.CdpClient(endpoint) as cdp:
        await cdp.call('Runtime.evaluate', expression="""document.body.innerHTML = `
          <div><input type="password" readonly onfocus="this.readOnly=false"><button>Sign in</button></div>`""")
    assert len((await browser_cdp.page_password_probe(endpoint, {})).visible_nodes) == 1


async def test_native_open_outcome_remains_cleanable(native_owner):
    """Also useful on hosts whose Chrome fails: the actual owner must still close."""
    result = await native_owner.run('native-outcome', ['open', 'about:blank'])
    assert isinstance(result, str)
    record = native_owner._sessions['native-outcome'].record
    assert record.binding is not None
    await native_owner.close_thread('native-outcome')
    assert identity.session_absent(native_owner.socket_dir, record.session)
    assert native_owner.live_count() == 0
    await native_owner.shutdown()


async def test_native_visible_session_uses_the_headed_launch_option(native_owner):
    assert not (await native_owner.run(
        "native-visible", ["open", "about:blank"], show_window=True,
    )).startswith("Error:")
    runtime = native_owner._sessions["native-visible"]
    assert "--headed" in native_owner._session_argv(runtime, ["snapshot"])
    await native_owner.close_thread("native-visible")


async def test_native_browser_success_failure_close_and_reopen(native_owner, tmp_path):
    owner = native_owner
    assert not (await owner.run('native', ['open', 'about:blank'])).startswith('Error:')
    record = owner._sessions['native'].record
    children = await asyncio.to_thread(psutil.Process(record.pid).children, recursive=True)
    assert children, 'successful local browser must have an observable child'
    for child in children:
        try:
            assert '--no-sandbox' not in child.cmdline()
        except psutil.NoSuchProcess:
            continue
    await owner.run('native', ['eval', "document.body.innerHTML='<button>Native proof</button>'; 'ready'"])
    await asyncio.sleep(1.15)  # Stable startup sidecars must survive reuse.
    assert 'Native proof' in await owner.run('native', ['snapshot'])
    with pytest.raises(BrowserSessionError, match='HTTP, HTTPS or about:blank'):
        await owner.run('native', ['open', 'http://[invalid'])
    assert (await owner.run('native', ['open', 'http://127.0.0.1:1/'])).startswith('Error:')
    assert 'ready' in await owner.run('native', ['eval', "'ready'"])
    screenshot = tmp_path / 'native.png'
    assert not (await owner.run('native', ['screenshot', str(screenshot)])).startswith('Error:')
    assert screenshot.read_bytes().startswith(b'\x89PNG')
    await owner.close_thread('native')
    assert identity.session_absent(owner.socket_dir, record.session)
    assert not (await owner.run('native', ['open', 'about:blank'])).startswith('Error:')
    assert owner._sessions['native'].record.session != record.session
    await owner.close_thread('native')
    await owner.shutdown()
    await owner.shutdown()


async def test_native_pane_fits_both_shapes_on_each_tab(native_owner, monkeypatch):
    thread, connection = 'native-fit', 'pane'
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, 'browser_sessions', lambda: native_owner)
    assert not (await native_owner.run(thread, ('open', 'about:blank'))).startswith('Error:')
    previous_endpoint = None
    try:
        for tab in range(2):
            if tab:
                assert not (await native_owner.run(thread, ('tab', 'new'))).startswith('Error:')
                browser_pane.clear_page_endpoint(thread, connection)
            endpoint = await native_owner.page_endpoint(thread)
            if previous_endpoint is not None:
                assert endpoint != previous_endpoint
            previous_endpoint = endpoint
            for width, height in ((700, 1400), (1400, 500)):
                await browser_pane.match_pane(thread, browser_pane.PaneAppearance(width, height, 1, 'dark'))
                async with browser_cdp.CdpClient(endpoint) as cdp:
                    metrics = await cdp.call('Page.getLayoutMetrics')
                viewport = metrics['cssLayoutViewport']
                assert (viewport['clientWidth'], viewport['clientHeight']) == (width, height)
                frames = asyncio.Queue()
                cast = browser_screencast.PaneScreencast(endpoint, lambda message: frames.put_nowait(json.loads(message)))
                cast.capture_at(width, height)
                running = asyncio.create_task(cast.run())
                try:
                    frame = await asyncio.wait_for(frames.get(), 10)
                    assert frame['data']
                    assert (frame['metadata']['deviceWidth'], frame['metadata']['deviceHeight']) == (width, height)
                finally:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
    finally:
        browser_pane.clear_page_endpoint(thread, connection)


async def test_native_pane_clipboard_selection_and_paste(native_owner, monkeypatch):
    thread, connection = 'native-clipboard', 'pane'
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, 'browser_sessions', lambda: native_owner)
    assert not (await native_owner.run(thread, ('open', 'about:blank'))).startswith('Error:')
    endpoint = await native_owner.page_endpoint(thread)
    try:
        async with browser_cdp.CdpClient(endpoint) as cdp:
            await cdp.call('Runtime.evaluate', expression=(
                "document.body.innerHTML = '<p id=source>Copied text</p><input id=destination>';"
                "const range = document.createRange(); range.selectNodeContents(document.querySelector('#source'));"
                "getSelection().removeAllRanges(); getSelection().addRange(range);"))
        replies = []
        await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': 1}, replies.append)
        assert replies == [{'type': 'clipboard_selection', 'id': 1, 'text': 'Copied text'}]
        async with browser_cdp.CdpClient(endpoint) as cdp:
            await cdp.call('Runtime.evaluate', expression="document.querySelector('#destination').focus()")
        await browser_clipboard.handle(thread, connection, {'type': 'clipboard_paste', 'text': 'Pasted text'}, replies.append)
        async with browser_cdp.CdpClient(endpoint) as cdp:
            result = await cdp.call('Runtime.evaluate', expression="document.querySelector('#destination').value", returnByValue=True)
        assert result['result']['value'] == 'Pasted text'
        assert len(replies) == 1
    finally:
        browser_pane.clear_page_endpoint(thread, connection)
