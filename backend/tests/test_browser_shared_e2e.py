"""Real agent-browser proof for shared page-context actions.

Run this host-only integration test through the isolated CORSIA test environment:
CORSIA_BROWSER_E2E=1 ... python -m pytest backend/tests/test_browser_shared_e2e.py -m integration
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import statistics
from threading import Thread
import tempfile
import time

from uuid import uuid4

import pytest

from host_access import (
    browser_cdp, browser_control, browser_dom_write, browser_pane_state, browser_screencast, browser_session_commands, browser_steps,
    browser_step_lifecycle as lifecycle,
)
from host_access.browser_session_state import BrowserSessionError
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

FORM = """<!doctype html><title>Form fixture</title>
<form><input id=field value=old><output id=result></output></form>
<script>
const field = document.querySelector('#field');
document.querySelector('form').addEventListener('submit', event => { event.preventDefault(); document.querySelector('#result').textContent = field.value; });
</script>"""

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


# Page-context writes: a dropdown, an SVG target, a rich-text editor, a covered button and one far
# below the fold, beside the person's own field.
WRITES = """<!doctype html><title>Writes fixture</title>
<input id=user value=person>
<select id=choice><option value=a>Ay</option><option value=b>Bee</option></select>
<svg width=60 height=40><rect id=shape width=60 height=40 fill=teal></rect></svg>
<div id=rich contenteditable>rich</div>
<div style="position:relative"><button id=covered>Under</button><div style="position:absolute;inset:0;background:white"></div></div>
<div style="height:3000px"></div><button id=far>Far</button>
<script>
window.clicks = {shape: 0, far: 0, covered: 0};
for (const id of Object.keys(window.clicks)) document.getElementById(id).addEventListener('click', () => { window.clicks[id]++; });
</script>"""

# Password fields the probe must find without reading the whole DOM: in a same-origin frame, in open
# and closed shadow roots, and named only by autocomplete, on a page whose DOM is over 1 MiB.
PROBE = """<!doctype html><title>Probe fixture</title>
<div id=bulk></div>
<iframe id=frame srcdoc="<input type=password id=inframe>"></iframe>
<div id=open-host></div><div id=closed-host></div>
<input id=auto autocomplete="username current-password">
<input type=password id=hidden style="display:none">
<script>
document.querySelector('#bulk').textContent = 'x'.repeat(1200000);
document.querySelector('#open-host').attachShadow({mode: 'open'}).innerHTML = '<input type=password id=inopen>';
document.querySelector('#closed-host').attachShadow({mode: 'closed'}).innerHTML = '<input type=password id=inclosed>';
</script>"""

# A one-document sign-in: submitting hides the password field and shows a code field.
SIGN_IN = """<!doctype html><title>Sign in</title>
<input id=user value=person><input type=password id=pw value=hunter2><input id=code value=123456 hidden>"""


@contextmanager
def fixture_server():
    pages = {"/plain.html": PLAIN.encode(), "/keyboard.html": KEYBOARD.encode(), "/form.html": FORM.encode(), "/react.html": REACT.encode(),
             "/writes.html": WRITES.encode(), "/probe.html": PROBE.encode(), "/sign-in.html": SIGN_IN.encode()}

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_pane_cdp_shortcut_sequence_submits_a_form():
    """A shortcut, text and submit delivered serially through CDP replace and submit one field."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-form-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-form", instance_key=uuid4().hex)
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/form.html"), show_window=True, session="new")).startswith("Error:")
            endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            for event_type, button, click_count in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 1)):
                await browser_cdp.dispatch_mouse_event(endpoint, {"eventType": event_type, "x": 20, "y": 20, "button": button, "clickCount": click_count})
            for event in (
                {"eventType": "keyDown", "key": "Meta", "code": "MetaLeft", "keyCode": 91, "text": "", "modifiers": 4},
                {"eventType": "keyDown", "key": "a", "code": "KeyA", "keyCode": 65, "text": "", "modifiers": 4},
                {"eventType": "keyUp", "key": "a", "code": "KeyA", "keyCode": 65, "text": "", "modifiers": 4},
                {"eventType": "keyUp", "key": "Meta", "code": "MetaLeft", "keyCode": 91, "text": "", "modifiers": 0},
                *({"eventType": kind, "key": letter, "code": f"Key{letter.upper()}", "keyCode": ord(letter.upper()), "text": letter if kind == "keyDown" else "", "modifiers": 0} for letter in "next" for kind in ("keyDown", "keyUp")),
                {"eventType": "keyDown", "key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r", "modifiers": 0},
                {"eventType": "keyUp", "key": "Enter", "code": "Enter", "keyCode": 13, "text": "", "modifiers": 0},
            ):
                await browser_cdp.dispatch_key_event(endpoint, event)
            assert await cdp_value(owner, thread_id, "({ value: field.value, result: document.querySelector('#result').textContent })") == {"value": "next", "result": "next"}
    finally:
        await owner.close_thread(thread_id)


def _quiet_password_steps(monkeypatch, steps: list[str]) -> None:
    """Record the person's password step instead of writing a plan, and report nothing to a pane."""
    async def open_step(_thread_id, step):
        lifecycle._steps[_thread_id] = step
        lifecycle._start_reprobe(_thread_id, step)
        step.node_id = "password-step"
        steps.append("opened")

    async def settle_step(_thread_id, _step):
        steps.append("settled")

    monkeypatch.setattr(lifecycle, "_step_row_state", lambda _node_id: None)
    monkeypatch.setattr(lifecycle, "_open_step", open_step)
    monkeypatch.setattr(lifecycle, "_settle_step", settle_step)
    monkeypatch.setattr(browser_steps, "publish_password_page", lambda *_args: None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_writes_select_click_svg_and_scroll_but_refuse_what_they_cannot_prove(monkeypatch):
    """AR1: each write applies in the page or is refused; none falls back to the person's input."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-writes-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-writes", instance_key=uuid4().hex)
    refusals = []
    monkeypatch.setattr(browser_session_commands, "emit_product_event",
                        lambda _surface, action, **kwargs: refusals.append((action, kwargs["properties"]["reason"])))
    browser_control.pane_connected(thread_id, "pane")
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/writes.html"), show_window=True, session="new")).startswith("Error:")
            await cdp_value(owner, thread_id, "document.querySelector('#user').focus(); document.querySelector('#user').setSelectionRange(1, 4); null")
            assert await owner.run(thread_id, ("select", "#choice", "Bee"), session="existing") == "ok"
            assert await owner.run(thread_id, ("click", "#shape"), session="existing") == "ok"
            assert await owner.run(thread_id, ("click", "#far"), session="existing") == "ok"
            with pytest.raises(browser_dom_write.DomWriteRefused, match="rich-text editor"):
                await owner.run(thread_id, ("fill", "#rich", "agent"), session="existing")
            with pytest.raises(browser_dom_write.DomWriteRefused, match="another element covers it"):
                await owner.run(thread_id, ("click", "#covered"), session="existing")
            with pytest.raises(browser_dom_write.DomWriteRefused, match="no option"):
                await owner.run(thread_id, ("select", "#choice", "Missing"), session="existing")
            page = await cdp_value(owner, thread_id, (
                "({ choice: document.querySelector('#choice').value, clicks: window.clicks,"
                " rich: document.querySelector('#rich').textContent, active: document.activeElement.id,"
                " start: document.querySelector('#user').selectionStart, end: document.querySelector('#user').selectionEnd })"))
            assert page == {"choice": "b", "clicks": {"shape": 1, "far": 1, "covered": 0}, "rich": "rich",
                            "active": "user", "start": 1, "end": 4}
            assert refusals == [("agent_write_refused", "rich_text"), ("agent_write_refused", "covered"),
                                ("agent_write_refused", "no_option")]
    finally:
        browser_control.pane_disconnected(thread_id, "pane")
        await owner.close_thread(thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_password_probe_finds_frame_and_shadow_fields_on_a_page_over_one_mebibyte(monkeypatch):
    """AR3: the probe reaches every in-process document and shadow root without the whole DOM."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-probe-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-probe", instance_key=uuid4().hex)
    steps = []
    _quiet_password_steps(monkeypatch, steps)
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/probe.html"), show_window=False, session="new")).startswith("Error:")
            # The pane's own resolver: the agent's read of this page would be refused.
            endpoint = await owner.page_endpoint(thread_id)
            async with browser_cdp.CdpClient(endpoint) as cdp:
                for _ in range(50):
                    loaded = await cdp.call("Runtime.evaluate", returnByValue=True, expression=(
                        "!!document.querySelector('#frame').contentDocument?.querySelector('#inframe')"))
                    if loaded["result"].get("value"):
                        break
                    await asyncio.sleep(0.1)
                size = await cdp.call("Runtime.evaluate", returnByValue=True,
                                      expression="document.documentElement.outerHTML.length")
            assert size["result"]["value"] > 2**20
            probe = await browser_cdp.page_password_probe(endpoint, {})
            # The frame's, both shadow roots' and the autocomplete-named field; never the hidden one.
            assert len(probe.visible_nodes) == 4, probe
            assert probe.target == endpoint
            # The agent is refused on this page and the person's step opens.
            with pytest.raises(BrowserSessionError, match="asks for a password"):
                await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            assert steps == ["opened"]
    finally:
        await owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_settled_password_visit_refuses_reads_of_its_hidden_field_by_ref_or_selector(monkeypatch):
    """AR2: once the field hides, a value or attribute read that locates it, or can't be located, is refused."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-read-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-read", instance_key=uuid4().hex)
    steps = []
    _quiet_password_steps(monkeypatch, steps)
    try:
        with fixture_server() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/sign-in.html"), show_window=False, session="new")).startswith("Error:")
            with pytest.raises(BrowserSessionError, match="asks for a password"):
                await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            # The person submits: the page hides the password field and shows a code field.
            endpoint = await owner.page_endpoint(thread_id)
            async with browser_cdp.CdpClient(endpoint) as cdp:
                await cdp.call("Runtime.evaluate", expression="pw.hidden = true; code.hidden = false; null")
            snapshot = await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            assert steps == ["opened", "settled"]
            refs = re.findall(r"textbox \[ref=(e\d+)\]", snapshot)
            assert len(refs) == 2, snapshot
            assert "123456" in await owner.run(thread_id, ("get", "value", f"@{refs[1]}"), session="existing")
            assert "person" in await owner.run(thread_id, ("get", "value", "#user"), session="existing")
            for command in (("get", "value", "#pw"), ("get", "attr", "#pw", "value"), ("get", "value", "input[type=password]"),
                            ("eval", "pw.value")):
                with pytest.raises(BrowserSessionError, match="asks for a password"):
                    await owner.run(thread_id, command, session="existing")
    finally:
        await owner.close_thread(thread_id)


@contextlib.asynccontextmanager
async def pane_screencast(endpoint: str):
    """Run the pane's own screencast of the page, yielding a reader of its next frame's metadata."""
    frames: asyncio.Queue[dict] = asyncio.Queue()
    cast = browser_screencast.PaneScreencast(endpoint, lambda message: frames.put_nowait(json.loads(message)))
    running = asyncio.create_task(cast.run())

    async def next_frame() -> dict:
        arrived = asyncio.ensure_future(frames.get())
        await asyncio.wait({arrived, running}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if not arrived.done():
            arrived.cancel()
            # A screencast that ended explains the missing frame better than a timeout.
            assert not running.done(), running.exception() if not running.cancelled() else "cancelled"
            raise AssertionError("no frame within 5 s")
        frame = arrived.result()
        assert frame["type"] == "frame" and frame["data"], frame
        return frame["metadata"]

    try:
        yield cast, frames, next_frame
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


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
            endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            async with pane_screencast(endpoint) as (_cast, frames, next_frame):
                # Starting a screencast sends the current picture without any page change.
                await next_frame()
                samples = []
                for index in range(15):
                    while not frames.empty():
                        frames.get_nowait()
                    started = time.perf_counter_ns()
                    await browser_cdp.dispatch_mouse_event(endpoint, {
                        "eventType": "mouseMoved", "x": 300 + index, "y": 24, "button": "none", "clickCount": 0})
                    await next_frame()
                    samples.append((time.perf_counter_ns() - started) / 1_000_000)
        ordered = sorted(samples)
        p95 = ordered[round((len(ordered) - 1) * .95)]
        print(f"input_to_frame_ms samples={len(samples)} median={statistics.median(samples):.1f} p95={p95:.1f}")
    finally:
        await owner.close_thread(thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pane_screencast_follows_a_viewport_change_and_an_agent_screenshot():
    """The pane keeps getting frames after its size changes and while the agent takes screenshots."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    thread_id = f"shared-screencast-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-shared-screencast", instance_key=uuid4().hex)
    try:
        with fixture_server() as origin:
            # Headless, as the pane's browser opens by default: a visible window under an emulated
            # viewport can skip the frame for a one-glyph change, which says nothing about the stream.
            assert not (await owner.run(thread_id, ("open", f"{origin}/plain.html"), show_window=False, session="new")).startswith("Error:")
            endpoint = page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            async with pane_screencast(endpoint) as (cast, frames, next_frame):
                shown = (await next_frame())["deviceWidth"]
                # Registered as the pane's cast, so the command path restarts it as it does in the app.
                browser_pane_state.screencast_started(thread_id, "pane", cast)
                try:
                    # 419 is narrower than Chrome's smallest window, so its frames hold the page beside
                    # the window's overflow; the pane must still get the page's own size.
                    for width, height in ((419, 653), (542, 845), (419, 745)):
                        cast.capture_at(width * 2, height * 2)
                        assert not (await owner.run(thread_id, ("set", "viewport", str(width), str(height), "2"), session="existing")).startswith("Error:")
                        # A frame sent before the command shows the page at its earlier size. Then the
                        # restart sends the current picture at once, at the new size, with no page change
                        # to prompt it.
                        while (seen := (await next_frame())["deviceWidth"]) != width:
                            assert seen == shown, (seen, shown, width)
                        shown = width
                        # The agent's screenshot still works mid-screencast, and the pane keeps streaming after it.
                        with tempfile.TemporaryDirectory() as folder:
                            shot = f"{folder}/agent.jpg"
                            assert not (await owner.run(thread_id, ("screenshot", shot), session="existing")).startswith("Error:")
                            assert os.path.getsize(shot) > 0
                        while not frames.empty():
                            frames.get_nowait()
                        await browser_cdp.dispatch_mouse_event(endpoint, {
                            "eventType": "mouseMoved", "x": 30 + width % 7, "y": 24, "button": "none", "clickCount": 0})
                        # A page change after the screenshot still reaches the pane, and the screenshot's
                        # temporary capture size never does.
                        assert (await next_frame())["deviceWidth"] == width
                finally:
                    browser_pane_state.screencast_ended(thread_id, "pane", cast)
    finally:
        await owner.close_thread(thread_id)
