import pytest

from host_access import browser_cdp
from host_access.browser_cli import CommandResult


@pytest.mark.asyncio
async def test_endpoint_resolves_agent_browsers_browser_url_to_the_page(monkeypatch):
    monkeypatch.setattr(browser_cdp, "_page_endpoint", lambda _url: "ws://page.test/devtools/page/1")

    async def run(_args, _timeout):
        return CommandResult(0, '{"data":{"cdpUrl":"ws://browser.test/devtools/browser/1"}}', "")

    assert await browser_cdp._endpoint(run) == "ws://page.test/devtools/page/1"


@pytest.mark.asyncio
async def test_box_accepts_agent_browser_031s_direct_data_shape():
    async def run(_args, _timeout):
        return CommandResult(0, '{"data":{"x":1,"y":2,"width":3,"height":4}}', "")

    assert await browser_cdp._box(run, "@e2") == {"x": 1, "y": 2, "width": 3, "height": 4}


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
