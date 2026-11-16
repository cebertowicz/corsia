"""A password field inside a cross-origin frame, on real Chrome: the step opens and the field's value stays guarded.

Runs only when CORSIA_TEST_CHROME names a Chrome or Chromium binary. It starts that Chrome headless on its own port
and profile, and drives the production probe, page guard and hit-test resolver against loopback fixture pages.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from host_access import browser_cdp, browser_control, browser_dom_write, browser_cdp_frames
from host_access.browser_session_state import BrowserSessionError
from tests.test_browser_person_step import THREAD, _steps, plan  # noqa: F401 - plan is a fixture

CHROME = os.environ.get("CORSIA_TEST_CHROME", "")
pytestmark = pytest.mark.skipif(not (CHROME and os.access(CHROME, os.X_OK)), reason="set CORSIA_TEST_CHROME to a Chrome binary")

# Each frame's field sits at a fixed place, so its box in the page is known without reading the frame: a sign-in
# form inside a larger frame, and a widget whose frame is exactly its field.
FIELD = {"form": (10, 10, 400, 200), "widget": (0, 0, 120, 24)}


def _frame(shape: str) -> str:
    left, top, _, _ = FIELD[shape]
    return (f'<html><body style="margin:0"><form><input id="pw" type="password" style="position:absolute; left:{left}px;'
            f' top:{top}px; width:120px; height:24px; margin:0; border:0; padding:0"></form>'
            "<script>addEventListener('hashchange', () => { document.getElementById('pw').style.display = 'none'; });</script>"
            "</body></html>")


def _page(frame_origin: str, shape: str) -> str:
    _, _, width, height = FIELD[shape]
    return (f'<html><body style="margin:0"><input id="q" type="text" style="position:absolute; left:10px; top:10px; width:120px; height:24px">'
            f'<iframe id="f" src="{frame_origin}/frame.html" style="position:absolute; left:0; top:60px; width:{width}px;'
            f' height:{height}px; border:0"></iframe></body></html>')


@contextmanager
def _sites(shape: str):
    """The page on 127.0.0.1 and its sign-in frame on localhost: two sites, so Chrome runs the frame out of process."""
    pages: dict[str, bytes] = {"/frame.html": _frame(shape).encode()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            body = pages.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    pages["/page.html"] = _page(f"http://localhost:{port}", shape).encode()
    try:
        yield f"http://127.0.0.1:{port}/page.html"
    finally:
        server.shutdown()


@contextmanager
def _chrome(url: str):
    """Headless Chrome on a free port and a throwaway profile; yields the page target's endpoint."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    profile = tempfile.mkdtemp(prefix="corsia-frame-guard-", dir="/tmp")
    # --no-sandbox: Chrome's own sandbox can't start inside a sandboxed test shell; only loopback fixtures load.
    process = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                                "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--disable-gpu",
                                "--site-per-process", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:  # nosec B310 - loopback
                    pages = [t for t in json.load(response) if t.get("type") == "page" and t.get("url") == url]
                if pages:
                    yield pages[0]["webSocketDebuggerUrl"]
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("Chrome did not open the fixture page")
            time.sleep(0.1)
    finally:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


async def _until(check, seconds: float = 15):
    deadline = time.monotonic() + seconds
    while True:
        if (result := await check()) is not None:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached on the live page")
        await asyncio.sleep(0.2)


async def _element_box(endpoint: str, element: str) -> dict:
    async with browser_cdp.CdpClient(endpoint) as cdp:
        rect = await cdp.call("Runtime.evaluate", returnByValue=True, expression=(
            f"(() => {{ const r = document.getElementById({json.dumps(element)}).getBoundingClientRect();"
            " return {x: r.x, y: r.y, width: r.width, height: r.height}; })()"))
    return rect["result"]["value"]


async def _locate(endpoint: str, box: dict) -> int | None:
    """What the read guard's resolver finds for an agent ref with `box`: the production hit test."""
    async with browser_cdp.CdpClient(endpoint) as cdp:
        return await browser_dom_write._node_for_box(cdp, box)


def _resolver(endpoint: str, box: dict):
    """The guard's resolve for one ref: it is called with the agent's ref and answers with the hit test."""
    async def resolve(_ref: str) -> int | None:
        return await _locate(endpoint, box)
    return resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(FIELD))
async def test_a_password_field_in_a_cross_origin_frame_opens_the_step_and_stays_guarded(plan, shape):  # noqa: F811
    with _sites(shape) as url, _chrome(url) as endpoint:
        probe = partial(browser_cdp.page_password_probe, endpoint)

        async def seen():
            found = await probe({})
            return found if found.visible_nodes else None

        found = await _until(seen)
        [key] = found.visible_nodes
        frame_element = await _element_box(endpoint, "f")
        assert browser_cdp_frames.split_key(key)[0] is not None, "the field is keyed inside its frame"

        # The step opens for the frame's field, so the page is the person's.
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access(THREAD, probe, ("snapshot",))
        assert [step["state"] for step in _steps(plan["db"])] == ["pending"]

        # The person signs in and the frame hides its field; the frame itself stays, and the step settles.
        async with browser_cdp.CdpClient(endpoint) as cdp:
            await cdp.call("Runtime.evaluate", expression="document.getElementById('f').src += '#done'")

        async def settled():
            try:
                return await browser_control.require_page_access(THREAD, probe, ("snapshot",)) or "ran"
            except BrowserSessionError:
                return None

        await _until(settled)

        # A ref on the frame's field: inside a larger frame nothing in the page matches its box, so it isn't located;
        # a frame exactly its field locates as the frame's element, which the guard holds for the field's visit.
        left, top, _, _ = FIELD[shape]
        field = {"x": frame_element["x"] + left, "y": frame_element["y"] + top, "width": 120, "height": 24}
        assert await _locate(endpoint, field) == (browser_cdp_frames.frame_owner(key) if shape == "widget" else None)
        resolve_field = _resolver(endpoint, field)
        for command in (("get", "value", "@e5"), ("get", "attr", "@e5", "value")):
            with pytest.raises(BrowserSessionError, match="stay refused"):
                await browser_control.require_page_access(THREAD, probe, command, resolve=resolve_field)
        with pytest.raises(BrowserSessionError, match="stay refused"):
            await browser_control.require_page_access(THREAD, probe, ("eval", "document.title"))

        # The page's own text field still reads.
        resolve_search = _resolver(endpoint, await _element_box(endpoint, "q"))
        assert await browser_control.require_page_access(THREAD, probe, ("get", "value", "@e1"), resolve=resolve_search) is None
