"""Explicit real-native job: no provider calls and no browser sandbox bypass.

Select with CORSIA_NATIVE_BROWSER_TEST=1 and absolute binary/Chrome paths.
Generic integration suites do not install or launch this optional native dependency.
"""
import asyncio
import os
from pathlib import Path
import shutil

import psutil
import pytest
from host_access import browser_cdp, browser_cli, browser_pane
from host_access import browser_identity as identity
from host_access.browser_sessions import BrowserSessionError, BrowserSessionOwner

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


async def test_pane_open_is_the_stock_clients_current_page(native_owner, monkeypatch, capsys):
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
        tabs_before = await native_owner.run('native-pane-open', ['tab'], session='existing', notice=False)
        owner_url = await native_owner.run('native-pane-open', ['get', 'url'], session='existing', notice=False)
        binding_after_owner = (await native_owner._observe(runtime)).binding
        result = await native_owner._invoke(native_owner._session_argv(runtime, ['get', 'url']), timeout=30)
        tabs_after = await native_owner.run('native-pane-open', ['tab'], session='existing', notice=False)
        binding_after_raw = (await native_owner._observe(runtime)).binding
        diagnostic = (f'pane open={opened!r}; tabs before={tabs_before!r}; agent get url={owner_url!r}; '
                      f'raw returncode={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}; '
                      f'tabs after={tabs_after!r}')
        with capsys.disabled():
            print(diagnostic)
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
