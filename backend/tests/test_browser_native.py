"""Explicit real-native job: no provider calls and no browser sandbox bypass.

Select with CORSIA_NATIVE_BROWSER_TEST=1 and absolute binary/Chrome paths.
Generic integration suites do not install or launch this optional native dependency.
"""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
from threading import Thread
from types import SimpleNamespace

import psutil
import pytest
from host_access import browser_identity as identity, browser_pane
from host_access.browser_sessions import BrowserSessionError, BrowserSessionOwner
from ws import browser_stream
from ws.browser_frames import PaneFrames

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


@contextmanager
def popup_fixture():
    pages = {
        '/': b'''<!doctype html><title>Opener</title><button id="open" onclick="window.open('/popup')">Sign in</button>
                  <output id="result"></output><script>window.addEventListener('message', e => {
                  document.querySelector('#result').textContent = e.data; });</script>''',
        '/popup': b'''<!doctype html><title>Popup</title><label>Account<input id="account"></label>
                       <button id="finish" onclick="window.opener.postMessage(document.querySelector('#account').value, '*');
                       window.close()">Finish</button>''',
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


async def _wait_for_pane_endpoint(thread, token, expected):
    async def wait():
        while browser_pane._page_endpoints.get((thread, token)) != expected:
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), 5)


async def test_native_pane_follows_popup_and_returns_to_opener(native_owner, monkeypatch):
    thread, token = 'native-popup', 'native-pane'
    monkeypatch.setattr(browser_pane, 'PROCESS_OWNER', SimpleNamespace(browser_sessions=lambda: native_owner))
    monkeypatch.setattr(browser_stream, 'emit_product_event', lambda *_args, **_kwargs: None)
    pane = None
    with popup_fixture() as origin:
        try:
            assert not (await native_owner.run(thread, ['open', origin])).startswith('Error:')
            opener = await native_owner.page_endpoint(thread)
            pane = asyncio.create_task(browser_stream._follow_page(thread, token, PaneFrames(), lambda _msg: None))
            await _wait_for_pane_endpoint(thread, token, opener)
            await asyncio.sleep(0.2)  # Allow the CDP target discovery replay to finish.
            assert not (await native_owner.run(thread, ['click', '#open'])).startswith('Error:')
            assert 'Account' in await native_owner.run(thread, ['snapshot'])
            popup = await native_owner.page_endpoint(thread)
            assert popup != opener
            await _wait_for_pane_endpoint(thread, token, popup)
            assert not (await native_owner.run(thread, ['fill', '#account', 'signed in'])).startswith('Error:')
            assert not (await native_owner.run(thread, ['click', '#finish'])).startswith('Error:')
            await _wait_for_pane_endpoint(thread, token, opener)
            assert 'signed in' in await native_owner.run(thread, ['snapshot'])
        finally:
            if pane:
                pane.cancel()
                await asyncio.gather(pane, return_exceptions=True)
            browser_pane.clear_page_endpoint(thread, token)
