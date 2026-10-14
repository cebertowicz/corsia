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
from threading import Thread
from uuid import uuid4

import pytest

from host_access import browser_cdp, browser_control
from host_access.browser_sessions import BrowserSessionOwner


PLAIN = """<!doctype html><title>Plain fixture</title>
<input id=user value=person><input id=agent><button id=agent-button>Do it</button><output id=result></output>
<script>
let pointer = null, clicks = 0;
document.addEventListener('pointermove', event => pointer = [event.clientX, event.clientY]);
document.querySelector('#agent-button').addEventListener('click', () => { clicks++; document.querySelector('#result').textContent = String(clicks); });
</script>"""

# This deliberately loads the real React UMD distribution: the controlled input exercises React's
# value-setter/event path rather than a lookalike implementation.
REACT = """<!doctype html><title>React fixture</title>
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<div id=root></div>
<script>
let pointer = null;
document.addEventListener('pointermove', event => pointer = [event.clientX, event.clientY]);
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
    pages = {"/plain.html": PLAIN.encode(), "/react.html": REACT.encode()}

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
