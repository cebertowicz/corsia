"""Per-pane aggregate stats: what is counted, what is validated, and that no content reaches the log."""
import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect
from loguru import logger

from infra.request_timing import _BUCKET_UPPERS, bucket_percentile
from tests.test_browser_pane import FakePane, screencast  # noqa: F401 - autouse fixture: no real page screencast
from ws import browser_pane_input, browser_pane_stats as stats_module, browser_stream
from ws.browser_frames import PaneFrames
from ws.browser_pane_stats import Histogram, PaneStats, input_kind, parse_report, viewer_kind

BUCKETS = len(_BUCKET_UPPERS)
FRAME = '{"type":"frame","data":"jpeg","metadata":{"deviceWidth":400,"deviceHeight":600}}'


@pytest.fixture
def lines():
    """Every 'browser pane stats' line logged while the test runs."""
    captured: list[str] = []
    handler = logger.add(lambda message: captured.append(message.record["message"]), level="INFO")
    yield captured
    logger.remove(handler)


def report(**changes):
    base = {"painted": 5, "painted_seq": 3, "up_buffered_max": 0, "visible": True,
            "decode_ms": [0] * BUCKETS, "input_to_paint_ms": [0] * BUCKETS}
    return {**base, **changes}


def token(line: str, name: str) -> str:
    return dict(part.split("=", 1) for part in line.split() if "=" in part)[name]


def test_histogram_percentiles_come_from_the_http_timing_buckets():
    histogram = Histogram()
    for value in (3, 3, 3, 3, 100):
        histogram.add(value)
    assert histogram.count == 5
    assert histogram.percentile(0.5) == bucket_percentile(histogram.buckets, 0.5)
    assert 2 < histogram.percentile(0.5) <= 4 and 64 < histogram.percentile(0.95) <= 128
    histogram.add(10**9)  # beyond the last bucket lands in it rather than raising
    assert histogram.buckets[-1] == 1


def test_a_report_must_be_whole_and_in_range():
    assert parse_report(report()) is not None
    for bad in (
        report(painted=-1), report(painted=True), report(painted=1.5), report(painted=10**6),
        report(painted_seq=2**31 + 1), report(up_buffered_max="9"), report(visible=1),
        report(decode_ms=[0] * (BUCKETS - 1)), report(input_to_paint_ms=[0] * (BUCKETS - 1) + [-1]),
        report(input_to_paint_ms="slow"), {"painted": 1},
    ):
        assert parse_report(bad) is None
    assert parse_report({k: v for k, v in report().items() if k != "visible"}) is None


def test_input_kinds():
    assert input_kind({"type": "input_mouse", "eventType": "mouseMoved"}) == "move"
    assert input_kind({"type": "input_mouse", "eventType": "mouseWheel"}) == "wheel"
    assert input_kind({"type": "input_mouse", "eventType": "mousePressed"}) == "click"
    assert input_kind({"type": "input_keyboard", "eventType": "keyDown"}) == "key"
    assert input_kind({"type": "navigate"}) is None


def test_a_frame_is_numbered_and_names_the_input_chrome_just_handled():
    stats = PaneStats("thread-1", "local")
    assert json.loads(stats.stamp(FRAME)) == {**json.loads(FRAME), "seq": 1}
    stats.input_handled({"type": "input_mouse", "eventType": "mousePressed", "id": 7}, 0.002)
    assert json.loads(stats.stamp(FRAME))["input"] == 7
    assert "input" not in json.loads(stats.stamp(FRAME))  # named once, by the first frame after it
    assert stats.sent_seq == 3


def test_only_input_that_can_change_the_picture_is_named():
    stats = PaneStats("thread-1", "local")
    for data in (
        {"type": "input_mouse", "eventType": "mouseMoved", "id": 4},
        {"type": "input_keyboard", "eventType": "keyDown", "id": 0},
        {"type": "input_keyboard", "eventType": "keyDown", "id": "9"},
        {"type": "input_keyboard", "eventType": "keyDown", "id": True},
        {"type": "input_keyboard", "eventType": "keyDown"},
    ):
        stats.input_handled(data, 0.001)
        assert "input" not in json.loads(stats.stamp(FRAME))


def test_a_frame_long_after_the_input_does_not_carry_its_name(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(stats_module.time, "monotonic", lambda: now[0])
    stats = PaneStats("thread-1", "local")
    stats.input_handled({"type": "input_keyboard", "eventType": "keyDown", "id": 3}, 0.001)
    now[0] += stats_module.STAMP_MAX_AGE_SECONDS + 0.1
    assert "input" not in json.loads(stats.stamp(FRAME))


def test_a_message_that_is_not_an_object_keeps_its_bytes():
    assert PaneStats("thread-1", "local").stamp("[1]") == "[1]"


def test_frames_in_flight_is_sent_minus_painted_at_each_report():
    stats = PaneStats("thread-1", "remote")
    for _ in range(5):
        stats.stamp(FRAME)
    stats.client_report(report(painted_seq=3))
    stats.client_report(report(painted_seq=5))
    assert stats._in_flight == [2, 0]
    stats.client_report(report(painted_seq=1))  # the newest painted frame never goes backwards
    assert stats._in_flight == [2, 0, 0]


def test_the_window_line_has_the_agreed_figures_and_no_content(lines):
    stats = PaneStats("0123456789abcdef", "remote")
    stats.appearance(browser_stream.browser_pane.PaneAppearance.parse(
        {"width": 1440, "height": 900, "scale": 2, "scheme": "dark"}))
    stats.frame_offered(300 * 1024, 0.4, replaced=False)
    stats.frame_offered(300 * 1024, 0.4, replaced=True)
    stats.stamp(FRAME)
    stats.frame_sent(12.0, 3 * 1024)
    stats.frame_sent(12.0, 1024)
    stats.input_handled({"type": "input_mouse", "eventType": "mouseMoved", "x": 123, "y": 456, "id": 1}, 0.01)
    stats.input_handled({"type": "input_mouse", "eventType": "mousePressed", "x": 123, "y": 456, "id": 2}, 0.02)
    stats.input_handled({"type": "input_mouse", "eventType": "mouseReleased", "x": 123, "y": 456, "id": 3}, 0.02)
    stats.input_handled({"type": "input_keyboard", "eventType": "keyDown", "key": "s3cret", "text": "s3cret"}, 0.03)
    stats.input_handled({"type": "input_mouse", "eventType": "mouseWheel", "deltaY": 99}, 0.02)
    stats.input_coalesced()
    stats.backlog(4)
    stats.loop_lag(2.0)
    delay = [0] * BUCKETS
    delay[5] = 3
    stats.client_report(report(painted=40, painted_seq=1, up_buffered_max=2048, input_to_paint_ms=delay, visible=False))
    stats.flush()

    (line,) = lines
    assert line.startswith("browser pane stats thread=01234567 ")
    for name, value in {
        "viewer": "remote", "size": "1440x900@2", "frames_in": "2", "replaced": "1", "sent": "2", "sent_kib": "4",
        "moves": "1", "clicks": "1", "keys": "1", "wheels": "1", "coalesced": "1", "backlog_max": "4",
        "reports": "1", "painted": "40", "in_flight_max": "0", "up_buffered_max": "2048",
        "input_to_paint_n": "3", "visible": "false",
    }.items():
        assert token(line, name) == value, name
    assert 16 < float(token(line, "input_to_paint_ms_p50")) <= 32
    assert 256 < float(token(line, "frame_kib_p50")) <= 512
    assert 16 < float(token(line, "dispatch_ms_wheel_p95")) <= 32
    names = {part.split("=", 1)[0] for part in line.split() if "=" in part}
    assert not names & {"x", "y", "key", "text", "url", "delta_y"} and "s3cret" not in line
    assert "final" not in line


def test_a_pane_with_no_report_logs_no_painted_figures_and_an_idle_window_logs_nothing(lines):
    stats = PaneStats("thread-1", "local")
    stats.flush()
    assert lines == []
    stats.frame_offered(1024, 0.1, replaced=False)
    stats.flush()
    assert token(lines[0], "reports") == "0" and "painted=" not in lines[0]
    stats.client_report({"painted": "many"})
    stats.flush()
    assert token(lines[1], "bad_reports") == "1"


def test_the_viewer_is_local_or_remote_and_the_address_is_not_kept():
    from starlette.websockets import WebSocket

    def pane(host, headers=()):
        scope = {"type": "websocket", "headers": [(k.encode(), v.encode()) for k, v in headers],
                 "client": (host, 1) if host else None}
        return WebSocket(scope, None, None)

    assert viewer_kind(pane("127.0.0.1")) == "local" and viewer_kind(pane("::1")) == "local"
    assert viewer_kind(pane("203.0.113.9")) == "remote"
    # A viewer relayed through a forwarder on this computer arrives from loopback with a forwarding header.
    for header in ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login"):
        assert viewer_kind(pane("127.0.0.1", [(header, "x")])) == "remote"
    assert viewer_kind(pane(None)) == "unknown" and viewer_kind(object()) == "unknown"


@pytest.mark.asyncio
async def test_the_ticker_runs_only_while_a_pane_is_connected(monkeypatch, lines):
    monkeypatch.setattr(stats_module, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(stats_module, "WINDOW_SECONDS", 0.05)
    assert stats_module._ticker is None
    first, second = PaneStats("thread-1", "local"), PaneStats("thread-2", "local")
    stats_module.start(first)
    ticker = stats_module._ticker
    stats_module.start(second)
    assert stats_module._ticker is ticker
    first.frame_offered(1024, 0.1, replaced=False)
    await asyncio.sleep(0.15)
    # The ticker flushed the first pane's window on its own, before the pane left.
    assert any("thread=thread-1" in line and not line.endswith(" final=1") for line in lines)
    stats_module.stop(first)
    assert not ticker.done() and stats_module._ticker is ticker
    stats_module.stop(second)
    await asyncio.sleep(0)
    assert stats_module._ticker is None and ticker.cancelled()
    stats_module.stop(second)  # stopping twice is harmless


@pytest.mark.asyncio
async def test_a_pane_that_leaves_logs_its_last_window_once(lines):
    stats = PaneStats("thread-1", "local")
    stats_module.start(stats)
    stats.frame_offered(1024, 0.1, replaced=False)
    stats_module.stop(stats)
    stats_module.stop(stats)
    assert len(lines) == 1 and lines[0].endswith(" final=1")


@pytest.mark.asyncio
async def test_frames_are_counted_replaced_numbered_and_timed():
    stats = PaneStats("thread-1", "local")
    frames = PaneFrames(stats)
    sent = []
    release = asyncio.Event()

    async def send(message):
        sent.append(json.loads(message))
        await release.wait()

    for index in range(3):
        frames.offer(json.dumps({"type": "frame", "data": index}))
    frames.control({"type": "navigate_result", "error": None})
    writer = asyncio.create_task(frames.send(send))
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0.01)
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)
    assert [m.get("seq") for m in sent] == [None, 1]  # control messages carry no number
    assert sent[1]["data"] == 2
    assert (stats._offered, stats._replaced, stats._sent) == (3, 2, 1)
    assert stats._frame_kib.count == 3 and stats._send_ms.count == 1


@pytest.mark.asyncio
async def test_the_input_reader_counts_coalesced_moves_and_the_waiting_backlog():
    move = lambda x: {"type": "input_mouse", "eventType": "mouseMoved", "x": x}  # noqa: E731
    click = {"type": "input_mouse", "eventType": "mousePressed", "x": 3}
    sent = asyncio.Queue()
    for message in (move(1), move(2), move(3), click, move(4)):
        sent.put_nowait(json.dumps(message))

    class Pane:
        async def receive_text(self):
            return await sent.get()

    stats = PaneStats("thread-1", "local")
    messages = browser_pane_input.pane_messages(Pane(), stats)
    assert [await anext(messages) for _ in range(3)] == [move(3), click, move(4)]
    await messages.aclose()
    assert stats._coalesced == 2 and stats._backlog_max == 3


@pytest.mark.asyncio
async def test_a_relayed_pane_logs_its_inputs_and_report_when_it_leaves(monkeypatch, lines):
    handled = []

    async def mouse_event(_thread, data, _token):
        handled.append(data)

    async def key_event(_thread, data, _token):
        handled.append(data)

    monkeypatch.setattr(browser_stream.browser_pane, "mouse_event", mouse_event)
    monkeypatch.setattr(browser_stream.browser_pane, "key_event", key_event)
    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", lambda *_args: asyncio.Future())
    pane = FakePane([json.dumps(message) for message in (
        {"type": "appearance", "width": 700, "height": 900, "scale": 1, "scheme": "dark"},
        {"type": "input_mouse", "eventType": "mousePressed", "x": 5, "y": 6, "id": 1},
        {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "id": 2},
        {"type": "pane_stats", **report(painted=9)},
        {"type": "pane_stats", "painted": "bad"},
    )])
    pane.client = type("Client", (), {"host": "203.0.113.9"})()
    pane.scope, pane.headers = {}, {}
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(browser_stream._relay(pane, "thread-1", "user-1"), 1)
    assert [data["id"] for data in handled] == [1, 2]
    (line,) = [line for line in lines if line.startswith("browser pane stats")]
    for name, value in {"viewer": "remote", "size": "700x900@1", "clicks": "1", "keys": "1",
                        "reports": "1", "painted": "9", "bad_reports": "1"}.items():
        assert token(line, name) == value, name
    assert line.endswith(" final=1") and stats_module._active == set()
    browser_stream._runners.clear()
