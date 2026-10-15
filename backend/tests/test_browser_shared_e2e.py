"""Real agent-browser proof for shared page-context actions.

Run this host-only integration test through the isolated CORSIA test environment:
CORSIA_BROWSER_E2E=1 ... python -m pytest backend/tests/test_browser_shared_e2e.py -m integration
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import statistics
from threading import Thread
import tempfile
import time

import websockets
from uuid import uuid4

import pytest

from host_access import browser_cdp, browser_control
from host_access.browser_sessions import BrowserSessionOwner


PLAIN = """<!doctype html><title>Plain fixture</title>
<input id=user value=person><input id=agent><button id=agent-button>Do it</button><output id=result></output><output id=frame></output>
<script>
let pointer = null, clicks = 0;
document.addEventListener('pointermove', event => { pointer = [event.clientX, event.clientY]; document.querySelector('#frame').textContent = pointer.join(','); });
document.querySelector('#agent-button').addEventListener('click', () => { clicks++; document.querySelector('#result').textContent = String(clicks); });
</script>"""

KEYBOARD = """<!doctype html><title>Keyboard fixture</title>
<input id=field value=old>"""

# This deliberately loads the real React UMD distribution: the controlled input exercises React's
# value-setter/event path rather than a lookalike implementation.
REACT = """<!doctype html><title>React fixture</title>
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<div id=root></div><output id=frame></output>
<script>
let pointer = null;
document.addEventListener('pointermove', event => { pointer = [event.clientX, event.clientY]; document.querySelector('#frame').textContent = pointer.join(','); });
window.addEventListener('load', () => {
  const h = React.createElement;
  function Fixture() {
    const [value, setValue] = React.useState(''); const [clicks, setClicks] = React.useState(0);
    return h(React.Fragment, null,
      h('input', {id: 'user', defaultValue: 'person'}),
      h('input', {id: 'agent', value, onChange: event => setValue(event.target.value)}),
      h('button', {id: 'agent-button', onClick: () => setClicks(clicks + 1)}, 'Do it'),
      h('output', {id: 'result'}, value + ':' + clicks));
  }
  ReactDOM.createRoot(document.querySelector('#root')).render(h(Fixture));
});
</script>"""


@contextmanager
def fixture_server():
    pages = {"/plain.html": PLAIN.encode(), "/keyboard.html": KEYBOARD.encode(), "/react.html": REACT.encode()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            body = pages.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def page_endpoint(rendered: str) -> str:
    value = json.loads(rendered).get("cdpUrl")
    assert isinstance(value, str) and value.startswith("ws"), rendered
    return browser_cdp._page_endpoint(value)


async def cdp_value(owner: BrowserSessionOwner, thread_id: str, expression: str):
    endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
    async with browser_cdp.CdpClient(endpoint) as cdp:
        result = await cdp.call("Runtime.evaluate", expression=expression, returnByValue=True)
    return result["result"].get("value")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(("fixture", "expected"), [("plain.html", "1"), ("react.html", "AB:1")])
async def test_shared_agent_actions_preserve_person_focus_caret_and_pointer(fixture, expected):
    """A pane-connected agent mutates its targets through CDP DOM calls, never user input."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-e2e-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-e2e", instance_key=uuid4().hex)
    browser_control.pane_connected(thread_id, "pane")
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/{fixture}"), show_window=True, session="new")).startswith("Error:")
            snapshot = await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            textboxes = re.findall(r"textbox \[ref=(e\d+)\]", snapshot)
            button = re.search(r"button \"Do it\" \[ref=(e\d+)\]", snapshot)
            assert len(textboxes) >= 2 and button is not None, snapshot
            agent_ref, button_ref = f"@{textboxes[1]}", f"@{button.group(1)}"
            # The person's caret is in their own field and their pointer rests at a distinct coordinate.
            await cdp_value(owner, thread_id, "document.querySelector('#user').focus(); document.querySelector('#user').setSelectionRange(1, 4); null")
            endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            async with browser_cdp.CdpClient(endpoint) as cdp:
                await cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=17, y=23)
            before = await cdp_value(owner, thread_id, "({ active: document.activeElement.id, start: document.querySelector('#user').selectionStart, end: document.querySelector('#user').selectionEnd, pointer })")

            assert await owner.run(thread_id, ("fill", agent_ref, "A"), session="existing") == "ok"
            assert await owner.run(thread_id, ("type", agent_ref, "B"), session="existing") == "ok"
            assert await owner.run(thread_id, ("click", button_ref), session="existing") == "ok"

            after = await cdp_value(owner, thread_id, "({ active: document.activeElement.id, start: document.querySelector('#user').selectionStart, end: document.querySelector('#user').selectionEnd, pointer, result: document.querySelector('#result').textContent })")
            assert before == {"active": "user", "start": 1, "end": 4, "pointer": [17, 23]}
            assert after == {**before, "result": expected}
    finally:
        browser_control.pane_disconnected(thread_id, "pane")
        await owner.close_thread(thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cdp_mouse_dispatch_clicks_the_visible_page():
    """The pane sends pointer input to Chrome's current page target."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-mouse-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-mouse", instance_key=uuid4().hex)
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/plain.html"), show_window=True, session="new")).startswith("Error:")
            endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            point = await cdp_value(owner, thread_id, "(() => { const box = document.querySelector('#agent-button').getBoundingClientRect(); return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; })()")
            for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
                await browser_cdp.dispatch_mouse_event(endpoint, {
                    "eventType": event_type, "x": point["x"], "y": point["y"],
                    "button": "none" if event_type == "mouseMoved" else "left",
                    "clickCount": 0 if event_type == "mouseMoved" else 1,
                })
            assert await cdp_value(owner, thread_id, "document.querySelector('#result').textContent") == "1"
    finally:
        await owner.close_thread(thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cdp_keyboard_dispatch_preserves_meta_shortcuts():
    """The shared pane bypasses agent-browser's modifier-less stream input."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-keyboard-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-keyboard", instance_key=uuid4().hex)
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/keyboard.html"), show_window=True, session="new")).startswith("Error:")
            endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            await cdp_value(owner, thread_id, "document.querySelector('#field').focus(); null")
            await browser_cdp.dispatch_key_event(endpoint, {"eventType": "keyDown", "key": "Meta", "code": "MetaLeft", "keyCode": 91, "text": "", "modifiers": 4})
            await browser_cdp.dispatch_key_event(endpoint, {"eventType": "keyDown", "key": "a", "code": "KeyA", "keyCode": 65, "text": "", "modifiers": 4})
            assert await cdp_value(owner, thread_id, "[field.selectionStart, field.selectionEnd]") == [0, 3]
    finally:
        await owner.close_thread(thread_id)


async def next_frame(stream) -> None:
    while True:
        message = await asyncio.wait_for(stream.recv(), timeout=5)
        if '"type":"frame"' in message or '"type": "frame"' in message:
            return


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_pane_input_to_frame_latency_reports_distribution():
    """Stage-one latency check: 15 visible pointer updates on the isolated shared-browser stack."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-latency-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-latency", instance_key=uuid4().hex)
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/plain.html"), show_window=True, session="new")).startswith("Error:")
            port = await owner.stream_port(thread_id)
            assert port.port is not None, port.error
            async with websockets.connect(f"ws://127.0.0.1:{port.port}", max_size=None) as stream:
                await stream.send('{"type":"screencast_start"}')
                # agent-browser may not repaint a static page until it receives a visual command.
                with tempfile.TemporaryDirectory() as folder:
                    assert not (await owner.run(thread_id, ("screenshot", f"{folder}/initial.jpg"), session="existing")).startswith("Error:")
                await next_frame(stream)
                samples = []
                for index in range(15):
                    started = time.perf_counter_ns()
                    await stream.send(f'{{"type":"input_mouse","eventType":"mouseMoved","x":{300 + index},"y":24,"button":"none","clickCount":0}}')
                    await next_frame(stream)
                    samples.append((time.perf_counter_ns() - started) / 1_000_000)
        ordered = sorted(samples)
        p95 = ordered[round((len(ordered) - 1) * .95)]
        print(f"input_to_frame_ms samples={len(samples)} median={statistics.median(samples):.1f} p95={p95:.1f}")
    finally:
        await owner.close_thread(thread_id)
