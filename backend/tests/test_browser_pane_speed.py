"""How fast the pane's own screencast follows real Chrome: an animated page and a scrolled page.

Run like the other real-browser tests: CORSIA_BROWSER_E2E=1 ... python -m pytest
backend/tests/test_browser_pane_speed.py -m integration -s

Each test reports its figures as one `PANE_SPEED` JSON line (and a table in the CI job summary) and
asserts only that the pane follows the page. The figures cover Chrome's capture, the server's relay
(`PaneScreencast` into `PaneFrames`) and pane input sent the way the pane sends it
(`browser_cdp.dispatch_mouse_event`); the network and the pane's decode and paint are not in them.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
import time
from uuid import uuid4

import pytest

from host_access import browser_cdp
from host_access.browser_screencast import PaneScreencast
from host_access.browser_sessions import BrowserSessionOwner
from ws.browser_frames import PaneFrames


ANIMATED = """<!doctype html><title>Animated</title>
<style>
body { margin: 0; font: 16px sans-serif; }
.bar { position: absolute; left: 0; width: 40vw; height: 40px; background: #36c; animation: slide 1.2s linear infinite alternate; }
.spin { position: absolute; top: 200px; left: 45vw; width: 160px; height: 160px; background: #c63; animation: spin 2s linear infinite; }
@keyframes slide { to { transform: translateX(60vw); } }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
<div class=bar style="top: 40px"></div><div class=bar style="top: 120px; animation-duration: .9s"></div><div class=spin></div>
<output id=tick style="position: absolute; top: 420px; left: 20px"></output>
<script>let n = 0; (function frame() { document.querySelector('#tick').textContent = String(n++); requestAnimationFrame(frame); })();</script>"""

LONG = """<!doctype html><title>Long</title>
<style>html { scroll-behavior: auto; } body { margin: 0; font: 16px/24px sans-serif; } p { margin: 0; padding: 0 12px; }
p:nth-child(odd) { background: #eef; }</style>
<main>""" + "".join(f"<p>Row {row}: the quick brown fox jumps over the lazy dog, row {row}.</p>" for row in range(1200)) + "</main>"

SINGLE_WHEELS = 15
SINGLE_GAP_SECONDS = 0.3
BURST_WHEELS = 60
BURST_GAP_SECONDS = 1 / 60
ANIMATION_SECONDS = 5.0
SHOWN_WITHIN_SECONDS = 5.0


@contextmanager
def _pages():
    pages = {"/animated.html": ANIMATED.encode(), "/long.html": LONG.encode()}

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


class _Relay:
    """The production relay without the socket: frames go through `PaneFrames` and are timed as sent."""

    def __init__(self) -> None:
        self.frames = PaneFrames()
        self.offered = 0
        self.offer_ms: list[float] = []
        self.sent: list[tuple[float, int, float | None]] = []  # (time, bytes, scrollOffsetY)
        self._new = asyncio.Event()

    def offer(self, message: str) -> None:
        started = time.perf_counter()
        self.frames.offer(message)
        self.offer_ms.append((time.perf_counter() - started) * 1000)
        self.offered += 1

    async def send_text(self, message: str) -> None:
        metadata = json.loads(message).get("metadata") or {}
        offset = metadata.get("scrollOffsetY")
        self.sent.append((time.perf_counter(), len(message), float(offset) if isinstance(offset, (int, float)) else None))
        self._new.set()
        await asyncio.sleep(0)

    async def frame_after(self, since: float, shows, timeout: float = SHOWN_WITHIN_SECONDS) -> float | None:
        """The time of the first frame sent after `since` whose scroll offset satisfies `shows`."""
        deadline = time.perf_counter() + timeout
        while True:
            for at, _size, offset in self.sent:
                if at >= since and offset is not None and shows(offset):
                    return at
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            self._new.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._new.wait(), remaining)

    def offset_now(self) -> float | None:
        return next((offset for _at, _size, offset in reversed(self.sent) if offset is not None), None)


def _page_endpoint(rendered: str) -> str:
    value = json.loads(rendered).get("cdpUrl")
    assert isinstance(value, str) and value.startswith("ws"), rendered
    return browser_cdp._page_endpoint(value)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 1)


def _report(name: str, figures: dict) -> None:
    print(f"PANE_SPEED {json.dumps({'case': name, **figures}, sort_keys=True)}")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as out:
            out.write(f"\n#### Pane speed: {name}\n\n| Figure | Value |\n|---|---|\n")
            out.writelines(f"| {key} | {value} |\n" for key, value in figures.items())


@contextlib.asynccontextmanager
async def _pane_on(fixture: str):
    thread_id = f"pane-speed-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-pane-speed", instance_key=uuid4().hex)
    relay = _Relay()
    tasks: list[asyncio.Task] = []
    try:
        with _pages() as origin:
            assert not (await owner.run(thread_id, ("open", f"{origin}/{fixture}"), show_window=True, session="new")).startswith("Error:")
            endpoint = _page_endpoint(await owner.run(thread_id, ("get", "cdp-url"), session="existing"))
            screencast = PaneScreencast(endpoint, relay.offer)
            tasks = [asyncio.create_task(screencast.run()), asyncio.create_task(relay.frames.send(relay.send_text))]
            assert await relay.frame_after(0, lambda _offset: True) is not None, "the pane never received a first frame"
            yield endpoint, relay
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await owner.close_thread(thread_id)


def _skip_unless_e2e() -> None:
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_animated_page_reaches_the_pane_as_a_stream_of_frames():
    _skip_unless_e2e()
    async with _pane_on("animated.html") as (_endpoint, relay):
        await asyncio.sleep(1.0)  # Past the first load's frames.
        start, offered, offer_from = time.perf_counter(), relay.offered, len(relay.offer_ms)
        await asyncio.sleep(ANIMATION_SECONDS)
        window = [(at, size) for at, size, _offset in relay.sent if at >= start]
        gaps = [(later - earlier) * 1000 for (earlier, _), (later, _) in zip(window, window[1:])]
        figures = {
            "frames_sent_per_s": round(len(window) / ANIMATION_SECONDS, 1),
            "frames_offered_per_s": round((relay.offered - offered) / ANIMATION_SECONDS, 1),
            "frame_gap_ms_p50": _percentile(gaps, .5), "frame_gap_ms_p95": _percentile(gaps, .95),
            "frame_kb_p50": _percentile([size / 1024 for _at, size in window], .5),
            "offer_ms_p50": _percentile(relay.offer_ms[offer_from:], .5), "offer_ms_p95": _percentile(relay.offer_ms[offer_from:], .95),
        }
        _report("animated page", figures)
        assert len(window) >= 2, "an animated page must keep reaching the pane"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_scrolled_page_reaches_the_pane_after_each_wheel_and_after_a_burst():
    _skip_unless_e2e()
    async with _pane_on("long.html") as (endpoint, relay):
        await asyncio.sleep(0.5)
        wheel = {"eventType": "mouseWheel", "x": 300, "y": 300, "deltaX": 0}
        # One wheel at a time: from sending it to the first frame that shows the page moved.
        dispatch_ms, shown_ms = [], []
        for _ in range(SINGLE_WHEELS):
            before = relay.offset_now()
            started = time.perf_counter()
            await browser_cdp.dispatch_mouse_event(endpoint, {**wheel, "deltaY": 120})
            dispatch_ms.append((time.perf_counter() - started) * 1000)
            shown = await relay.frame_after(started, lambda offset, before=before: offset != before)
            assert shown is not None, f"a wheel's scroll never reached the pane (offset stayed {before})"
            shown_ms.append((shown - started) * 1000)
            await asyncio.sleep(SINGLE_GAP_SECONDS)
        # A trackpad-like burst: one wheel per display frame, as the pane sends them.
        burst_start, burst_from = time.perf_counter(), len(relay.sent)
        burst_dispatch_ms = []
        for _ in range(BURST_WHEELS):
            started = time.perf_counter()
            await browser_cdp.dispatch_mouse_event(endpoint, {**wheel, "deltaY": 40})
            burst_dispatch_ms.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(max(0.0, BURST_GAP_SECONDS - (time.perf_counter() - started)))
        burst_end = time.perf_counter()
        async with browser_cdp.CdpClient(endpoint) as cdp:
            settled = (await cdp.call("Runtime.evaluate", expression="window.scrollY", returnByValue=True))["result"]["value"]
        caught_up = await relay.frame_after(burst_start, lambda offset: abs(offset - settled) < 1)
        assert caught_up is not None, f"the pane never showed where the burst left the page ({settled})"
        burst_frames = len([1 for at, _size, _offset in relay.sent[burst_from:] if at <= burst_end])
        figures = {
            "wheel_to_frame_ms_p50": _percentile(shown_ms, .5), "wheel_to_frame_ms_max": _percentile(shown_ms, 1),
            "wheel_dispatch_ms_p50": _percentile(dispatch_ms, .5), "wheel_dispatch_ms_p95": _percentile(dispatch_ms, .95),
            "burst_wheels_per_s": round(BURST_WHEELS / (burst_end - burst_start), 1),
            "burst_dispatch_ms_p95": _percentile(burst_dispatch_ms, .95),
            "burst_frames_per_s": round(burst_frames / (burst_end - burst_start), 1),
            "burst_end_to_last_frame_ms": round(max(0.0, caught_up - burst_end) * 1000, 1),
        }
        _report("scrolled page", figures)
