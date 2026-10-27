"""Synthetic listener fixtures and page-route checks shared across browser platforms."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from host_access import browser_cdp, browser_ports

_SENTINEL = 'DISPOSABLE_LISTENER_OK'


async def _serve(reader, writer, requests):
    try:
        raw = await reader.readuntil(b'\r\n\r\n')
        lines = raw.decode().split('\r\n')
        path = lines[0].split()[1]
        headers = {k.lower(): v for line in lines[1:] if ': ' in line for k, v in [line.split(': ', 1)]}
        websocket = headers.get('upgrade', '').lower() == 'websocket'
        requests.append((path, websocket))
        if websocket:
            key = headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
            writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                          'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
            await writer.drain()
            await asyncio.wait_for(reader.read(1024), 5)
        elif path.startswith('/redirect?'):
            target = parse_qs(urlsplit(path).query)['target'][0]
            writer.write(('HTTP/1.1 302 Found\r\nLocation: ' + target + '\r\n'
                          'Content-Length: 0\r\nConnection: close\r\n\r\n').encode())
        else:
            body = f'<!doctype html><title>{_SENTINEL}</title><body>{_SENTINEL}</body>'.encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nAccess-Control-Allow-Origin: *\r\n'
                         b'Connection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


@contextlib.asynccontextmanager
async def listener_pair(ports=(10080, 6566)):
    """Own one restricted and one unrestricted throwaway listener, never another session."""
    requests = {'restricted': [], 'allowed': []}
    restricted = None
    for port in ports:
        try:
            restricted = await asyncio.start_server(
                lambda r, w: _serve(r, w, requests['restricted']), '127.0.0.1', port)
            break
        except OSError:
            continue
    assert restricted is not None, 'no free restricted port for the disposable listener'
    allowed = None
    try:
        allowed = await asyncio.start_server(lambda r, w: _serve(r, w, requests['allowed']), '127.0.0.1', 0)
        restricted_port = restricted.sockets[0].getsockname()[1]
        allowed_port = allowed.sockets[0].getsockname()[1]
        assert allowed_port not in browser_ports.RESERVED_PORTS
        yield restricted_port, allowed_port, requests
    finally:
        restricted.close()
        await restricted.wait_closed()
        if allowed is not None:
            allowed.close()
            await allowed.wait_closed()


@pytest.fixture
async def restricted_listeners():
    """The ordinary one-port fixture shared with the managed macOS session test."""
    async with listener_pair() as listeners:
        yield listeners


async def _evaluate(cdp, expression):
    result = await cdp.call('Runtime.evaluate', expression=expression, awaitPromise=True, returnByValue=True)
    assert 'exceptionDetails' not in result, result.get('exceptionDetails')
    return result['result'].get('value')


async def _popup(browser, page, url):
    before = {t['targetId'] for t in (await browser.call('Target.getTargets'))['targetInfos']}
    await _evaluate(page, f'window.open({json.dumps(url)}); true')
    for _ in range(60):
        created = [t for t in (await browser.call('Target.getTargets'))['targetInfos']
                   if t['type'] == 'page' and t['targetId'] not in before]
        if created:
            assert len(created) == 1
            target = created[0]['targetId']
            return target, page.endpoint.rsplit('/', 1)[0] + '/' + target
        await asyncio.sleep(.05)
    pytest.fail('window.open did not create a page target')


async def _title(cdp):
    for _ in range(60):
        value = await _evaluate(cdp, '({title:document.title,ready:document.readyState,url:document.URL})')
        if value['ready'] == 'complete' and value['url'] != 'about:blank':
            return value['title']
        await asyncio.sleep(.05)
    pytest.fail('fixture document did not finish loading')


async def _matrix(owner, cdp, phase, allowed, restricted):
    results = {}
    for label, port in [('allowed', allowed), ('restricted', restricted)]:
        target = f'http://127.0.0.1:{port}/{phase}-{label}'
        for route, url in [('open', target), ('redirect',
                f'http://127.0.0.1:{allowed}/redirect?target={quote(target, safe="")}')]:
            if owner is not None:
                outcome = await owner.run('proof', ('open', url))
                blocked = 'ERR_UNSAFE_PORT' in outcome
                if label == 'allowed':
                    assert not outcome.startswith('Error'), outcome
            else:
                outcome = await cdp.call('Page.navigate', url=url)
                blocked = outcome.get('errorText') == 'net::ERR_UNSAFE_PORT'
            assert blocked == (label == 'restricted'), (phase, route, label, outcome)
            if not blocked:
                assert await _title(cdp) == _SENTINEL
            results[f'{label}_{route}'] = 'refused' if blocked else 'loaded'
        await cdp.call('Page.navigate', url=f'http://127.0.0.1:{allowed}/page')
        assert await _title(cdp) == _SENTINEL
        fetch = await _evaluate(cdp, '(async()=>{try {const r=await fetch(' + json.dumps(target) +
                                ');return {status:"read",body:await r.text()}}'
                                'catch(e){return {status:"refused",error:String(e)}}})()')
        assert (fetch['status'] == 'refused') == (label == 'restricted'), fetch
        if label == 'allowed':
            assert _SENTINEL in fetch['body']
        results[f'{label}_fetch'] = fetch['status']
        ws_url = f'ws://127.0.0.1:{port}/{phase}-ws'
        ws = await _evaluate(cdp, 'new Promise(resolve=>{try {const s=new WebSocket(' +
            json.dumps(ws_url) + ');let done=false;const finish=status=>{if(!done){done=true;'
            's.close();resolve({status})}};s.onopen=()=>finish("opened");'
            's.onerror=()=>finish("refused");setTimeout(()=>finish("timeout"),3000)}'
            'catch(e){resolve({status:"refused",error:String(e)})}})')
        assert ws['status'] == ('refused' if label == 'restricted' else 'opened'), ws
        results[f'{label}_websocket'] = ws['status']
    return results


async def assert_restricted_routes(endpoint, listeners, *, owner=None):
    """Exercise the same navigation, redirect, fetch, WebSocket and later-popup matrix."""
    restricted, allowed, requests = listeners
    page_endpoint = await asyncio.to_thread(browser_cdp._page_endpoint, endpoint)
    results = {}
    async with browser_cdp.CdpClient(endpoint) as browser, browser_cdp.CdpClient(page_endpoint) as page:
        results['chrome'] = (await browser.call('Browser.getVersion'))['product']
        results['main'] = await _matrix(owner, page, 'main', allowed, restricted)
        assert not requests['restricted'], 'page traffic reached the restricted fixture listener'
        target, popup_endpoint = await _popup(browser, page, f'http://127.0.0.1:{restricted}/popup-refused')
        async with browser_cdp.CdpClient(popup_endpoint) as popup:
            assert await _title(popup) != _SENTINEL
        assert not requests['restricted']
        await browser.call('Target.closeTarget', targetId=target)
        target, popup_endpoint = await _popup(browser, page, f'http://127.0.0.1:{allowed}/popup-allowed')
        async with browser_cdp.CdpClient(popup_endpoint) as popup:
            assert await _title(popup) == _SENTINEL
            results['popup'] = await _matrix(None, popup, 'popup', allowed, restricted)
        await browser.call('Target.closeTarget', targetId=target)
    assert not requests['restricted']
    assert sum(ws for _, ws in requests['allowed']) == 2
    results['restricted_listener_requests'] = requests['restricted']
    results['allowed_websocket_handshakes'] = sum(ws for _, ws in requests['allowed'])
    return results
