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
from host_access import browser_cdp, browser_cli, browser_pane, browser_screencast
from host_access import browser_identity as identity
from host_access.browser_sessions import BrowserSessionError, BrowserSessionOwner
from ws import browser_clipboard, browser_stream
from ws.browser_frames import PaneFrames

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('CORSIA_NATIVE_BROWSER_TEST') != '1', reason='explicit native-browser job only',
)]

# The run's first Chrome launch on a cold hosted runner once took 32.7 s, past run()'s 30 s
# default; later launches take seconds. Warm it once so no test's own budget pays for it.
# Best effort, tried once: a Chrome that cannot launch fails in the tests that cover it.
COLD_START_SECONDS = 90.0
_warmed: list[bool] = []


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
        if not _warmed:
            _warmed.append(True)
            await owner.run('native-warm-up', ['open', 'about:blank'], timeout=COLD_START_SECONDS)
            await owner.close_thread('native-warm-up')
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
    async def serve(reader, writer):
        # Read the whole request first: closing with unread bytes resets the connection instead.
        await reader.readuntil(b'\r\n\r\n')
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
        # Read through the owner on the existing session only, so a relaunch fails instead of hiding a lost
        # binding. Like BrowserOpenTool, suppress the one-time notice before parsing get url.
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


@pytest.mark.parametrize('headed', [False, True])
async def test_native_pane_clipboard_copy_cut_and_paste(native_owner, monkeypatch, headed):
    thread, connection = 'native-clipboard', 'pane'
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, 'browser_sessions', lambda: native_owner)
    with popup_fixture() as base:
        assert not (await native_owner.run(thread, ('open', base), show_window=headed)).startswith('Error:')
        endpoint = await native_owner.page_endpoint(thread)
        async with browser_cdp.CdpClient(endpoint) as preload:
            await preload.call('Page.enable')
            await preload.call('Page.addScriptToEvaluateOnNewDocument', source=browser_screencast.PRELOAD,
                               worldName=browser_screencast.WORLD, runImmediately=True)
            await _exercise_clipboard(thread, connection, endpoint)


async def _exercise_clipboard(thread, connection, endpoint):
    try:
        async with browser_cdp.CdpClient(endpoint) as cdp:
            await cdp.call('Runtime.evaluate', expression=(
                "document.body.innerHTML = '<p id=source>Copied text</p><input id=destination>';"
                "window.clipboardEvents = []; for (const kind of ['copy', 'cut', 'paste']) document.addEventListener(kind, e => clipboardEvents.push(e.type));"
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
        async with browser_cdp.CdpClient(endpoint) as cdp:
            result = await cdp.call('Runtime.evaluate', expression='clipboardEvents', returnByValue=True)
        assert result['result']['value'] == ['copy', 'paste']
        # A successful paste reports no error: it clears any the pane is showing.
        assert replies[1:] == [{'type': 'pane_error', 'source': 'clipboard', 'error': None}]
        await _check_page_handled_clipboard(thread, connection, endpoint)
        # Same-origin frame selection and cut operate in that frame's own document.
        async with browser_cdp.CdpClient(endpoint) as cdp:
            await cdp.call('Runtime.evaluate', expression=(
                "document.body.innerHTML = '<iframe></iframe>';"
                "const frame = document.querySelector('iframe');"
                "frame.contentDocument.body.innerHTML = '<input value=\"Cut me here\">';"
                "for (const kind of ['copy', 'cut', 'paste']) frame.contentDocument.addEventListener(kind, e => clipboardEvents.push('frame-' + e.type));"
                "const input = frame.contentDocument.querySelector('input'); input.focus(); input.setSelectionRange(0, 7);"))
        await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': 11}, replies.append)
        await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': 2, 'cut': True}, replies.append)
        async with browser_cdp.CdpClient(endpoint) as cdp:
            result = await cdp.call('Runtime.evaluate', expression="document.querySelector('iframe').contentDocument.querySelector('input').value", returnByValue=True)
        assert result['result']['value'] == 'here'
        assert replies[2:] == [{'type': 'clipboard_selection', 'id': 11, 'text': 'Cut me '},
                               {'type': 'clipboard_selection', 'id': 2, 'text': 'Cut me '}]
        await browser_clipboard.handle(thread, connection, {'type': 'clipboard_paste', 'text': 'frame paste'}, replies.append)
        async with browser_cdp.CdpClient(endpoint) as cdp:
            result = await cdp.call('Runtime.evaluate', expression='clipboardEvents.filter(x => x.startsWith("frame-"))', returnByValue=True)
        assert result['result']['value'] == ['frame-copy', 'frame-cut', 'frame-paste']
        # Cross-origin copy and paste are exercised by the separate OOPIF test.
        # Cut deletes only the copied selection, in a text field and in an editable region.
        for request, setup, read in (
                # Each setup is a block: top-level const persists across evaluates and would redeclare.
                (3, "{ document.body.innerHTML = '<input id=field value=\"Cut me here\">';"
                    "const field = document.querySelector('#field'); field.focus(); field.setSelectionRange(0, 7); }",
                 "document.querySelector('#field').value"),
                (4, "{ document.body.innerHTML = '<div id=editor contenteditable>Cut me here</div>';"
                    "const editor = document.querySelector('#editor'); editor.focus();"
                    "const range = document.createRange(); range.setStart(editor.firstChild, 0); range.setEnd(editor.firstChild, 7);"
                    "getSelection().removeAllRanges(); getSelection().addRange(range); }",
                 "document.querySelector('#editor').textContent")):
            async with browser_cdp.CdpClient(endpoint) as cdp:
                assert 'exceptionDetails' not in await cdp.call('Runtime.evaluate', expression=setup)
            replies.clear()
            await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': request, 'cut': True}, replies.append)
            async with browser_cdp.CdpClient(endpoint) as cdp:
                result = await cdp.call('Runtime.evaluate', expression=read, returnByValue=True)
            assert replies == [{'type': 'clipboard_selection', 'id': request, 'text': 'Cut me '}]
            assert result['result']['value'] == 'here'
        async with browser_cdp.CdpClient(endpoint) as cdp:
            result = await cdp.call('Runtime.evaluate', expression='clipboardEvents.filter(x => x === "cut").length', returnByValue=True)
        assert result['result']['value'] == 2
    finally:
        browser_pane.clear_page_endpoint(thread, connection)


async def _check_page_handled_clipboard(thread, connection, endpoint):
    for stop in (False, True):
        async with browser_cdp.CdpClient(endpoint) as cdp:
            expression = """{ document.activeElement.blur(); const r = document.createRange(); r.selectNodeContents(document.querySelector('#source'));
              getSelection().removeAllRanges(); getSelection().addRange(r);
              document.addEventListener('copy', e => { e.clipboardData.setData('text/plain', 'custom page text');
              e.preventDefault(); %s }, {once: true}); }""" % ('e.stopPropagation();' if stop else '')
            await cdp.call('Runtime.evaluate', expression=expression)
        handled = []
        await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': 10}, handled.append)
        if stop:
            assert handled[0].get('error')
        else:
            assert handled == [{'type': 'clipboard_selection', 'id': 10, 'text': 'custom page text'}]


@contextmanager
def popup_fixture():
    pages = {
        '/': b'''<!doctype html><title>Opener</title><button id="open" onclick="window.open('/popup')">Sign in</button>
                  <output id="result"></output><script>window.addEventListener('message', e => {
                  document.querySelector('#result').textContent = e.data; });</script>''',
        # Close after the click returns: a synchronous close can tear the target down before
        # the click's own mouse-release is acknowledged, which the stock client reports as a timeout.
        '/clipboard-child': b'''<!doctype html><input id="field" value="Cross origin">
            <script>const field = document.querySelector('#field');
            window.addEventListener('message', () => { field.focus(); field.select(); parent.postMessage('ready', '*'); });
            field.addEventListener('paste', e => parent.postMessage('paste:' + e.clipboardData.getData('text/plain'), '*'));
            </script>''',
        '/popup': b'''<!doctype html><title>Popup</title><label>Account<input id="account"></label>
                       <button id="finish" onclick="window.opener.postMessage(document.querySelector('#account').value, '*');
                       setTimeout(() => window.close())">Finish</button>''',
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


@pytest.mark.parametrize('headed', [False, True])
async def test_native_cross_origin_clipboard_and_page_read_denial(native_owner, monkeypatch, headed):
    thread, connection = 'native-clipboard-oopif', 'pane'
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, 'browser_sessions', lambda: native_owner)
    with popup_fixture() as base:
        assert not (await native_owner.run(thread, ('open', base), show_window=headed)).startswith('Error:')
        endpoint = await native_owner.page_endpoint(thread)
        try:
            async with browser_cdp.CdpClient(endpoint) as cdp:
                await cdp.call('Page.enable')
                await cdp.call('Page.addScriptToEvaluateOnNewDocument', source=browser_screencast.PRELOAD,
                               worldName=browser_screencast.WORLD, runImmediately=True)
                probe = "navigator.clipboard.readText().then(() => 'allowed', () => 'denied')"
                async def read_probe():
                    result = await cdp.call('Runtime.evaluate', expression=probe, awaitPromise=True, returnByValue=True)
                    return result['result']['value']
                assert await read_probe() == 'denied'
                await cdp.call('Browser.grantPermissions', origin=base, permissions=['clipboardSanitizedWrite'])
                try:
                    assert await read_probe() == 'denied'
                finally:
                    await cdp.call('Browser.resetPermissions')
                assert await read_probe() == 'denied'
                child = base.replace('127.0.0.1', 'localhost') + '/clipboard-child'
                setup = """new Promise((resolve, reject) => {
                    window.clipboardEvents = [];
                    window.addEventListener('message', e => { clipboardEvents.push(e.data); if (e.data === 'ready') resolve(true); });
                    const frame = document.createElement('iframe'); frame.src = %s;
                    frame.onload = () => frame.contentWindow.postMessage('focus', '*'); document.body.append(frame);
                    setTimeout(() => reject(Error('frame not ready')), 5000);
                })""" % json.dumps(child)
                result = await cdp.call('Runtime.evaluate', expression=setup, awaitPromise=True, returnByValue=True)
                assert result['result']['value'] is True
                replies = []
                await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': 1}, replies.append)
                assert replies[0].get('error')
                await browser_clipboard.handle(thread, connection, {'type': 'clipboard_selection', 'id': 2, 'cut': True}, replies.append)
                assert replies[1].get('error')
                await browser_clipboard.handle(thread, connection, {'type': 'clipboard_undo'}, replies.append)
                assert replies[2] == {'type': 'pane_error', 'source': 'clipboard', 'error': None}
                await browser_clipboard.handle(thread, connection, {'type': 'clipboard_paste', 'text': 'cross paste'}, replies.append)
                result = await cdp.call('Runtime.evaluate', expression="new Promise(resolve => { const poll = () => clipboardEvents.includes('paste:cross paste') ? resolve(true) : setTimeout(poll, 20); poll(); setTimeout(() => resolve(false), 5000); })", awaitPromise=True, returnByValue=True)
                assert result['result']['value'] is True
        finally:
            browser_pane.clear_page_endpoint(thread, connection)


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
