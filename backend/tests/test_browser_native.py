"""Explicit real-native job: no provider calls and no browser sandbox bypass.

Select with CORSIA_NATIVE_BROWSER_TEST=1 and absolute binary/Chrome paths.
Generic integration suites do not install or launch this optional native dependency.
"""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import psutil
import pytest
from host_access import browser_cdp, browser_cli, browser_pane
from host_access import browser_identity as identity
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


async def test_pane_open_is_the_stock_clients_current_page(native_owner, monkeypatch):
    async def serve(_reader, writer):
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\nBrowser test')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    url = f'http://127.0.0.1:{port}/'
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, 'browser_sessions', lambda: native_owner)
    try:
        opened = await browser_pane.open_url('native-pane-open', url)
        assert not opened.startswith('Error:'), f'pane open={opened!r}'
        runtime = native_owner._sessions['native-pane-open']
        binding = runtime.record.binding
        assert binding is not None, f'pane open={opened!r}'
        # BrowserOpenTool suppresses the one-time notice before parsing get url.
        owner_url = await native_owner.run('native-pane-open', ['get', 'url'], session='existing', notice=False)
        binding_after_owner = (await native_owner._observe(runtime)).binding
        result = await native_owner._invoke(native_owner._session_argv(runtime, ['get', 'url']), timeout=30)
        binding_after_raw = (await native_owner._observe(runtime)).binding
        diagnostic = (f'pane open={opened!r}; agent get url={owner_url!r}; '
                      f'raw returncode={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}')
        assert browser_cli.json_object(owner_url).get('url') == url, diagnostic
        assert binding_after_owner == binding, diagnostic
        assert result.returncode == 0, diagnostic
        data = browser_cli.json_object(result.stdout).get('data', {})
        assert data.get('lifecycle', {}).get('reused') is True, diagnostic
        assert data.get('lifecycle', {}).get('launched') is False, diagnostic
        assert binding_after_raw == binding, diagnostic
        assert data.get('url') == url, diagnostic
    finally:
        server.close()
        await server.wait_closed()


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


def _capture_target_watch(monkeypatch):
    """Expose each target watch's known pages, which it fills once Chrome has replayed the existing targets."""
    watched = []
    real = browser_pane.watch_targets

    def watch(thread_id, notify, known):
        watched.append(known)
        return real(thread_id, notify, known)
    monkeypatch.setattr(browser_pane, 'watch_targets', watch)
    return watched


async def _wait_for_target_discovery(watched):
    async def wait():
        while not watched or watched[-1][0] is None:
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), 5)


async def test_native_pane_follows_popup_and_returns_to_opener(native_owner, monkeypatch):
    thread, token = 'native-popup', 'native-pane'
    monkeypatch.setattr(browser_pane, 'PROCESS_OWNER', SimpleNamespace(browser_sessions=lambda: native_owner))
    monkeypatch.setattr(browser_stream, 'emit_product_event', lambda *_args, **_kwargs: None)
    watched = _capture_target_watch(monkeypatch)
    pane = None
    with popup_fixture() as origin:
        try:
            assert not (await native_owner.run(thread, ['open', origin])).startswith('Error:')
            opener = await native_owner.page_endpoint(thread)
            pane = asyncio.create_task(browser_stream._follow_page(thread, token, PaneFrames(), lambda _msg: None))
            await _wait_for_pane_endpoint(thread, token, opener)
            await _wait_for_target_discovery(watched)
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


@contextmanager
def signin_popup_fixture(variant):
    # Both origins share a tiny test-only completion store, standing in for a server-mediated sign-in.
    state = {'value': ''}
    servers = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlsplit(self.path)
            if route.path == '/complete':
                state['value'] = parse_qs(route.query).get('value', [''])[0]
                body = b'ok'
            elif route.path == '/result':
                body = state['value'].encode()
            elif route.path == '/popup':
                body = b'''<!doctype html><title>Popup</title><label>Account<input id="account"></label>
                <button id="finish" onclick="fetch('/complete?value='+encodeURIComponent(document.querySelector('#account').value))
                  .then(() => window.close())">Finish</button>'''
            elif route.path == '/':
                port = servers[1].server_port if variant in {'cross-origin', 'coop'} else servers[0].server_port
                url = json.dumps(f'http://127.0.0.1:{port}/popup')
                opening = (f"const w=window.open('', 'g_auth', 'popup,width=500,height=600'); w.location={url};"
                           if variant == 'blank-navigation' else
                           f"window.open({url}, 'g_auth', 'popup,width=500,height=600');")
                body = f'''<!doctype html><title>Opener</title><input id="control"><button id="open">Sign in</button>
                <output id="result"></output><script>
                document.querySelector('#open').onclick = () => {{ {opening} }};
                setInterval(async () => {{ document.querySelector('#result').textContent =
                    await (await fetch('/result')).text(); }}, 100);
                </script>'''.encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            if variant == 'coop' and route.path in {'/', '/popup'}:
                self.send_header('Cross-Origin-Opener-Policy',
                                 'same-origin-allow-popups' if route.path == '/' else 'same-origin')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    servers.extend(ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2))
    threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
    for worker in threads:
        worker.start()
    try:
        yield f'http://127.0.0.1:{servers[0].server_port}'
    finally:
        for server in servers:
            server.shutdown()
        for worker in threads:
            worker.join()
        for server in servers:
            server.server_close()


def _click_expression(selector):
    return (f"(() => {{ const element = document.querySelector({json.dumps(selector)}); "
            "if (!element) return null; const r = element.getBoundingClientRect(); "
            "return [r.x + r.width / 2, r.y + r.height / 2]; })()")


async def _pane_click(thread, token, selector):
    endpoint = await browser_pane._page_endpoint(thread, token, 'pane unavailable')
    async with browser_cdp.CdpClient(endpoint) as cdp:
        async def coordinates():
            while True:
                result = await cdp.call('Runtime.evaluate', expression=_click_expression(selector), returnByValue=True)
                assert 'exceptionDetails' not in result, f"coordinate lookup failed for {selector}: {result}"
                remote = result.get('result', {})
                if remote.get('subtype') != 'null':
                    value = remote.get('value')
                    assert isinstance(value, list) and len(value) == 2, f"invalid coordinates for {selector}: {result}"
                    return value
                await asyncio.sleep(0.05)
        try:
            x, y = await asyncio.wait_for(coordinates(), 5)
        except TimeoutError as exc:
            raise AssertionError(f'element {selector} did not appear in the pane') from exc
    for event_type in ('mousePressed', 'mouseReleased'):
        await browser_pane.mouse_event(thread, {'eventType': event_type, 'x': x, 'y': y,
                                                'button': 'left', 'clickCount': 1}, token)


async def _pane_type_a(thread, token):
    # Match BrowserLeaf's keydown/keyup messages and keyText for a printable key.
    for event_type, text in (('keyDown', 'a'), ('keyUp', '')):
        await browser_pane.key_event(thread, {'eventType': event_type, 'key': 'a', 'code': 'KeyA',
                                               'keyCode': 65, 'text': text, 'modifiers': 0}, token)


@pytest.mark.parametrize('variant', ['features', 'cross-origin', 'coop', 'blank-navigation'])
async def test_native_pane_signin_popup_variants(native_owner, monkeypatch, variant):
    thread, token = 'popup-variants', 'variant-pane'
    monkeypatch.setattr(browser_pane, 'PROCESS_OWNER', SimpleNamespace(browser_sessions=lambda: native_owner))
    monkeypatch.setattr(browser_stream, 'emit_product_event', lambda *_args, **_kwargs: None)
    watched = _capture_target_watch(monkeypatch)
    pane = None
    with signin_popup_fixture(variant) as origin:
        try:
            assert not (await native_owner.run(thread, ['open', origin])).startswith('Error:')
            opener = await native_owner.page_endpoint(thread)
            pane = asyncio.create_task(browser_stream._follow_page(thread, token, PaneFrames(), lambda _msg: None))
            await _wait_for_pane_endpoint(thread, token, opener)
            await _wait_for_target_discovery(watched)
            await _pane_click(thread, token, '#control')
            await _pane_type_a(thread, token)
            async with browser_cdp.CdpClient(opener) as cdp:
                control = await cdp.call('Runtime.evaluate', expression="document.querySelector('#control').value",
                                         returnByValue=True)
            assert control.get('result', {}).get('value') == 'a', f'opener keyboard control failed: {control}'
            await _pane_click(thread, token, '#open')
            assert 'Account' in await native_owner.run(thread, ['snapshot'])
            popup = await native_owner.page_endpoint(thread)
            assert popup != opener
            await _wait_for_pane_endpoint(thread, token, popup)
            if variant == 'coop':
                async with browser_cdp.CdpClient(popup) as cdp:
                    result = await cdp.call('Runtime.evaluate', expression='window.opener === null', returnByValue=True)
                assert result['result']['value'], 'COOP variant should sever the opener reference'
            await _pane_click(thread, token, '#account')
            await _pane_type_a(thread, token)
            await _pane_click(thread, token, '#finish')
            await _wait_for_pane_endpoint(thread, token, opener)
            async def posted():
                while True:
                    async with browser_cdp.CdpClient(opener) as cdp:
                        result = await cdp.call('Runtime.evaluate', expression="document.querySelector('#result').textContent",
                                                returnByValue=True)
                    if result.get('result', {}).get('value') == 'a':
                        return
                    await asyncio.sleep(0.1)
            await asyncio.wait_for(posted(), 5)
            assert await native_owner.page_endpoint(thread) == opener
        finally:
            if pane:
                pane.cancel()
                await asyncio.gather(pane, return_exceptions=True)
            browser_pane.clear_page_endpoint(thread, token)
