import io
import json
import logging

import pytest

from host_access import browser_cdp, browser_clipboard, browser_dom_write
from host_access.browser_cli import CommandResult


@pytest.mark.asyncio
async def test_cdp_error_logs_diagnostics_and_keeps_person_facing_error(caplog):
    class Socket:
        async def send(self, _message):
            pass

        async def recv(self):
            return json.dumps({'id': 1, 'error': {'code': -32602, 'message': 'invalid parameters'}})

    caplog.set_level(logging.DEBUG, logger=browser_cdp.__name__)
    client = browser_cdp.CdpClient('ws://page')
    client.socket = Socket()
    with pytest.raises(browser_cdp.BrowserSessionError, match="This page couldn't be checked"):
        await client.call('Input.dispatchKeyEvent', text='a')
    assert 'Input.dispatchKeyEvent' in caplog.text
    assert '-32602' in caplog.text and 'invalid parameters' in caplog.text


@pytest.mark.asyncio
async def test_cdp_records_parent_links_from_pushed_search_paths():
    class Socket:
        def __init__(self):
            self.responses = iter([
                {"method": "DOM.setChildNodes", "params": {"parentId": 1, "nodes": [{"nodeId": 2}]}},
                {"method": "DOM.setChildNodes", "params": {"parentId": 2, "nodes": [{"nodeId": 3}]}},
                {"id": 1, "result": {"nodeIds": [3]}},
            ])

        async def send(self, _data):
            pass

        async def recv(self):
            return json.dumps(next(self.responses))

    client = browser_cdp.CdpClient("ws://fixture")
    client.socket = Socket()
    assert await client.call("DOM.getSearchResults") == {"nodeIds": [3]}
    assert client.parents == {2: 1, 3: 2}


@pytest.mark.asyncio
async def test_page_endpoint_resolves_the_browser_url_to_the_page_the_session_shows(monkeypatch):
    resolved = []
    monkeypatch.setattr(browser_cdp, "_page_endpoint",
                        lambda browser, url, title: resolved.append((browser, url, title)) or "ws://page.test/devtools/page/1")
    outputs = {
        ("get", "cdp-url"): '{"data":{"cdpUrl":"ws://browser.test/devtools/browser/1"}}',
        ("get", "url"): '{"data":{"url":"https://example.test/current"}}',
        ("get", "title"): '{"data":{"title":"Current"}}',
    }

    async def run(args, _timeout):
        return CommandResult(0, outputs[tuple(args)], "")

    assert await browser_cdp.page_endpoint(run) == "ws://page.test/devtools/page/1"
    assert resolved == [("ws://browser.test/devtools/browser/1", "https://example.test/current", "Current")]


@pytest.mark.asyncio
async def test_page_endpoint_fails_closed_without_a_browser_url():
    async def run(_args, _timeout):
        return CommandResult(1, "", "no browser")

    with pytest.raises(browser_cdp.BrowserSessionError):
        await browser_cdp.page_endpoint(run)


@pytest.mark.parametrize(("node", "password"), [
    ({"nodeName": "INPUT", "attributes": ["type", "Password"]}, True),
    ({"nodeName": "INPUT", "attributes": ["type", "text", "autocomplete", "username current-password"]}, True),
    ({"nodeName": "INPUT", "attributes": ["autocomplete", "new-password"]}, True),
    # The search also matches text and other elements that only mention a password.
    ({"nodeName": "LABEL", "attributes": ["for", "password"]}, False),
    ({"nodeName": "INPUT", "attributes": ["name", "password", "type", "text"]}, False),
    ({"nodeName": "#text", "nodeValue": "Forgot password?"}, False),
])
def test_only_a_password_input_counts_as_a_password_field(node, password):
    assert browser_cdp._is_password_input(node) is password


def test_page_endpoint_prefers_the_sessions_current_page(monkeypatch):
    targets = [
        {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://page.test/devtools/page/blank"},
        {"type": "page", "url": "https://example.test/current", "title": "Other", "webSocketDebuggerUrl": "ws://page.test/devtools/page/other"},
        {"type": "page", "url": "https://example.test/current", "title": "Current", "webSocketDebuggerUrl": "ws://page.test/devtools/page/current"},
    ]

    class Response:
        def __enter__(self):
            return io.BytesIO(json.dumps(targets).encode())

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(browser_cdp, "urlopen", lambda *_args, **_kwargs: Response())
    assert browser_cdp._page_endpoint("ws://browser.test/devtools/browser/1", "https://example.test/current", "Current") == "ws://page.test/devtools/page/current"


@pytest.mark.asyncio
async def test_restricted_port_fixture_waits_for_closed_popup_to_disappear(monkeypatch):
    from tests.browser_restricted_ports_fixture import _close_popup

    calls = []

    class Browser:
        async def call(self, method, **params):
            calls.append((method, params))
            if method == 'Target.getTargets':
                return {'targetInfos': ([{'targetId': 'popup'}] if len(calls) == 2 else [])}
            return {'success': True}

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr('tests.browser_restricted_ports_fixture.asyncio.sleep', no_delay)
    await _close_popup(Browser(), 'popup')
    assert [method for method, _ in calls] == ['Target.closeTarget', 'Target.getTargets', 'Target.getTargets']


@pytest.mark.asyncio
async def test_box_accepts_agent_browser_031s_direct_data_shape():
    async def run(_args, _timeout):
        return CommandResult(0, '{"data":{"x":1,"y":2,"width":3,"height":4}}', "")

    assert await browser_dom_write._box(run, "@e2") == {"x": 1, "y": 2, "width": 3, "height": 4}


@pytest.mark.asyncio
async def test_dispatch_mouse_event_preserves_wheel_axes(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            calls.append((method, params))
            return {}

    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _endpoint: Client())
    await browser_cdp.dispatch_mouse_event("websocket-endpoint", {
        "eventType": "mouseWheel", "x": 209, "y": 220, "deltaX": 0, "deltaY": 640, "deltaMode": 0,
    })

    assert calls == [("Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": 209, "y": 220, "deltaX": 0, "deltaY": 640,
    })]


@pytest.mark.asyncio
async def test_dispatch_mouse_event_sets_the_pressed_button_mask(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def call(self, method, **params):
            calls.append((method, params))
            return {}

    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _endpoint: Client())
    await browser_cdp.dispatch_mouse_event("websocket-endpoint", {
        "eventType": "mousePressed", "x": 10, "y": 20, "button": "left", "clickCount": 1,
    })
    await browser_cdp.dispatch_mouse_event("websocket-endpoint", {
        "eventType": "mouseReleased", "x": 10, "y": 20, "button": "left", "clickCount": 1,
    })
    assert calls == [
        ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 10, "y": 20, "button": "left", "buttons": 1, "clickCount": 1}),
        ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 10, "y": 20, "button": "left", "buttons": 0, "clickCount": 1}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(('button', 'mask'), [('right', 2), ('middle', 4)])
async def test_dispatch_mouse_event_uses_the_pressed_button_mask(monkeypatch, button, mask):
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def call(self, method, **params):
            calls.append(params)
            return {}
    monkeypatch.setattr(browser_cdp, 'CdpClient', lambda _endpoint: Client())
    await browser_cdp.dispatch_mouse_event('endpoint', {
        'eventType': 'mousePressed', 'x': 10, 'y': 20, 'button': button, 'clickCount': 1,
    })
    assert calls[0]['buttons'] == mask
    await browser_cdp.dispatch_mouse_event('endpoint', {
        'eventType': 'mouseMoved', 'x': 15, 'y': 20, 'button': button, 'clickCount': 0,
    })
    assert calls[1]['buttons'] == mask


@pytest.mark.asyncio
async def test_clipboard_selection_and_paste_use_native_commands(monkeypatch):
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def call(self, method, **params):
            calls.append((method, params))
            if method == 'Page.getFrameTree': return {'frameTree': {'frame': {'id': 'top', 'url': 'https://example.com/'}}}
            if method == 'Page.createIsolatedWorld': return {'executionContextId': 7}
            if method == 'Runtime.evaluate':
                value = 0 if '|| 0' in params['expression'] else {'kind': 'copy', 'prevented': False, 'selection': 'selected'}
                return {'result': {'value': value}}
            return {}
    monkeypatch.setattr(browser_clipboard, 'CdpClient', lambda _endpoint, **_kwargs: Client())
    assert await browser_clipboard.clipboard_selection('endpoint', False) == 'selected'
    await browser_clipboard.clipboard_paste('endpoint', 'paste text')
    commands = [params['commands'][0] for method, params in calls if method == 'Input.dispatchKeyEvent' and 'commands' in params]
    assert commands == ['copy', 'paste']
    grants = [params['permissions'] for method, params in calls if method == 'Browser.grantPermissions']
    assert grants == [['clipboardSanitizedWrite']]
    assert any(method == 'Browser.resetPermissions' for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(('platform', 'modifier'), [('darwin', 4), ('linux', 2)])
async def test_clipboard_commands_use_native_platform_modifier(monkeypatch, platform, modifier):
    calls = []
    class Client:
        async def call(self, method, **params): calls.append((method, params))
    monkeypatch.setattr(browser_clipboard.sys, 'platform', platform)
    await browser_clipboard._command(Client(), 'cut')
    assert [params['type'] for _, params in calls] == ['keyDown', 'keyUp']
    assert all(params['modifiers'] == modifier for _, params in calls)
    assert calls[0][1]['commands'] == ['cut']


@pytest.mark.asyncio
async def test_page_handled_cut_does_not_return_plain_selection(monkeypatch):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def call(self, method, **params):
            if method == 'Page.getFrameTree': return {'frameTree': {'frame': {'id': 'top', 'url': 'https://example.com/'}}}
            if method == 'Page.createIsolatedWorld': return {'executionContextId': 7}
            if method == 'Runtime.evaluate':
                value = 0 if '|| 0' in params['expression'] else {'kind': 'cut', 'prevented': True, 'data': '', 'selection': 'wrong'}
                return {'result': {'value': value}}
            return {}
    monkeypatch.setattr(browser_clipboard, 'CdpClient', lambda _endpoint, **_kwargs: Client())
    with pytest.raises(browser_clipboard.PageHandledError):
        await browser_clipboard.clipboard_selection('endpoint', True)


@pytest.mark.asyncio
async def test_clipboard_missing_capture_fails(monkeypatch):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def call(self, method, **params):
            if method == 'Page.getFrameTree': return {'frameTree': {'frame': {'id': 'top', 'url': 'https://example.com/'}}}
            if method == 'Page.createIsolatedWorld': return {'executionContextId': 7}
            if method == 'Runtime.evaluate': return {'result': {'value': 0 if '|| 0' in params['expression'] else None}}
            return {}
    monkeypatch.setattr(browser_clipboard, 'CdpClient', lambda _endpoint, **_kwargs: Client())
    with pytest.raises(browser_clipboard.BrowserSessionError):
        await browser_clipboard.clipboard_selection('endpoint', False)


@pytest.mark.asyncio
async def test_dispatch_key_event_preserves_meta_modifier_and_text(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            calls.append((method, params))
            return {}

    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _endpoint: Client())

    await browser_cdp.dispatch_key_event("websocket-endpoint", {
        "eventType": "keyDown", "key": "a", "code": "KeyA", "keyCode": 65, "text": "", "modifiers": 4,
    })
    await browser_cdp.dispatch_key_event("websocket-endpoint", {
        "eventType": "keyDown", "key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r", "modifiers": 0,
    })

    assert calls == [
        ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 4, "commands": ["selectAll"]}),
        ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0, "text": "\r"}),
    ]
