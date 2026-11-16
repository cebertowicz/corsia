import asyncio
import json

import pytest

from host_access import browser_screencast


class ScreencastSocket:
    """A page target that answers screencast commands and sends the frames a test queues."""

    def __init__(self, log):
        self.log = log
        self.incoming = asyncio.Queue()
        self.closed = False
        self.layout = {"cssLayoutViewport": {"clientWidth": 1000, "clientHeight": 1000}}
        self.measurements = None  # A list here keeps each measurement's answer until the test sends it.

    async def send(self, text):
        message = json.loads(text)
        self.log.append(("cdp", message["method"], message["params"]))
        result = self.layout if message["method"] == "Page.getLayoutMetrics" else {}
        answer = {"id": message["id"], "result": result}
        if message["method"] == "Page.getLayoutMetrics" and self.measurements is not None:
            self.measurements.append(answer)
        else:
            self.incoming.put_nowait(answer)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)

    async def close(self):
        self.closed = True


def _frame(session, width):
    return {"method": "Page.screencastFrame", "params": {
        "sessionId": session, "data": f"jpeg-{session}", "metadata": {"deviceWidth": width, "deviceHeight": 600}}}


async def _started(log):
    while not any(entry[1] == "Page.startScreencast" for entry in log):
        await asyncio.sleep(0)


def _screencast(monkeypatch, log):
    socket = ScreencastSocket(log)
    connected = []

    async def connect(endpoint, **kwargs):
        connected.append((endpoint, kwargs["max_size"]))
        return socket

    monkeypatch.setattr(browser_screencast.websockets, "connect", connect)
    cast = browser_screencast.PaneScreencast("ws://page.test/devtools/page/1", lambda message: log.append(("pane", json.loads(message))))
    return cast, socket, connected


@pytest.mark.asyncio
async def test_pane_screencast_acknowledges_each_frame_before_relaying_it(monkeypatch):
    log = []
    cast, socket, connected = _screencast(monkeypatch, log)
    running = asyncio.create_task(cast.run())
    await _started(log)
    for message in (_frame(1, 400), _frame(2, 400), None):
        socket.incoming.put_nowait(message)
    await asyncio.wait_for(running, 1)
    # Frames are read uncapped: a high-density frame exceeds the request client's 1 MiB limit.
    assert connected == [("ws://page.test/devtools/page/1", None)]
    assert log == [
        ("cdp", "Page.getLayoutMetrics", {}),
        ("cdp", "Page.enable", {}),
        ("cdp", "Page.addScriptToEvaluateOnNewDocument", {"source": browser_screencast.PRELOAD, "worldName": browser_screencast.WORLD, "runImmediately": True}),
        ("cdp", "Page.startScreencast", {"format": "jpeg", "quality": 80}),
        ("cdp", "Page.captureScreenshot", {"format": "jpeg", "quality": 10}),
        ("cdp", "Page.screencastFrameAck", {"sessionId": 1}),
        ("pane", {"type": "frame", "data": "jpeg-1", "metadata": {"deviceWidth": 400, "deviceHeight": 600}}),
        ("cdp", "Page.screencastFrameAck", {"sessionId": 2}),
        ("pane", {"type": "frame", "data": "jpeg-2", "metadata": {"deviceWidth": 400, "deviceHeight": 600}}),
    ]
    assert socket.closed


@pytest.mark.asyncio
async def test_compact_screencast_uses_the_pane_pixels_and_stable_compact_quality(monkeypatch):
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    cast.capture_at(786, 1704, compact=True)
    running = asyncio.create_task(cast.run())
    await _started(log)
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)
    assert log[3] == ("cdp", "Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 786, "maxHeight": 1704})
    # Returning to a wider pane restores its normal quality on the existing viewport restart.
    cast.capture_at(1360, 2060)
    assert cast._quality() == 80


@pytest.mark.asyncio
async def test_pane_screencast_restarts_at_the_new_device_size(monkeypatch):
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    running = asyncio.create_task(cast.run())
    await _started(log)
    socket.incoming.put_nowait(_frame(1, 400))
    while not any(entry[0] == "pane" for entry in log):
        await asyncio.sleep(0)
    cast.capture_at(1360, 2060)
    await cast.restart()
    socket.incoming.put_nowait(_frame(2, 680))
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)
    assert [entry[1] if entry[0] == "cdp" else entry[1]["data"] for entry in log] == [
        "Page.getLayoutMetrics", "Page.enable", "Page.addScriptToEvaluateOnNewDocument", "Page.startScreencast", "Page.captureScreenshot", "Page.screencastFrameAck", "jpeg-1",
        "Page.stopScreencast", "Page.getLayoutMetrics", "Page.startScreencast", "Page.captureScreenshot", "Page.screencastFrameAck", "jpeg-2",
    ]
    assert log[9] == ("cdp", "Page.startScreencast", {"format": "jpeg", "quality": 80, "maxWidth": 1360, "maxHeight": 2060})


@pytest.mark.asyncio
async def test_a_held_pane_screencast_shows_no_frame_until_its_restart_measures_the_page(monkeypatch):
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    running = asyncio.create_task(cast.run())
    await _started(log)
    cast.hold()
    # A page that loads mid-command leaves the restart to the command.
    socket.incoming.put_nowait({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
    for _ in range(10):
        await asyncio.sleep(0)
    assert socket.incoming.empty()
    # Mid-command frames, such as a screenshot's temporary capture size, are acknowledged, not shown.
    socket.incoming.put_nowait(_frame(1, 1084))
    while not any(entry[2] == {"sessionId": 1} for entry in log if entry[0] == "cdp"):
        await asyncio.sleep(0)
    socket.layout = {"cssLayoutViewport": {"clientWidth": 419, "clientHeight": 653}}
    await cast.restart()
    socket.incoming.put_nowait(_frame(2, 500))
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)
    assert [entry[1] for entry in log if entry[0] == "pane"] == [
        {"type": "frame", "data": "jpeg-2", "metadata": {"deviceWidth": 419, "deviceHeight": 600}}]


@pytest.mark.asyncio
async def test_pane_screencast_sized_before_it_starts_captures_at_that_size(monkeypatch):
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    cast.capture_at(800, 600)
    await cast.restart()  # Not capturing yet: run() starts at the new size.
    running = asyncio.create_task(cast.run())
    await _started(log)
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)
    assert log == [("cdp", "Page.getLayoutMetrics", {}), ("cdp", "Page.enable", {}),
                   ("cdp", "Page.addScriptToEvaluateOnNewDocument", {"source": browser_screencast.PRELOAD, "worldName": browser_screencast.WORLD, "runImmediately": True}),
                   ("cdp", "Page.startScreencast", {"format": "jpeg", "quality": 80, "maxWidth": 800, "maxHeight": 600}),
                   ("cdp", "Page.captureScreenshot", {"format": "jpeg", "quality": 10})]


@pytest.mark.asyncio
async def test_pane_screencast_restarts_once_a_new_document_loads_so_its_first_picture_is_shown(monkeypatch):
    # Chrome can capture none of a new document's first pictures, and a still page draws no more.
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    running = asyncio.create_task(cast.run())
    await _started(log)
    socket.incoming.put_nowait(_frame(1, 400))
    socket.incoming.put_nowait({"method": "Page.frameNavigated", "params": {"frame": {"id": "main"}}})
    socket.incoming.put_nowait({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})

    async def restarted():
        while sum(entry[1] == "Page.startScreencast" for entry in log) < 2:
            await asyncio.sleep(0)

    await asyncio.wait_for(restarted(), 1)
    socket.incoming.put_nowait(_frame(2, 400))
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)
    assert [entry[1] if entry[0] == "cdp" else entry[1]["data"] for entry in log] == [
        "Page.getLayoutMetrics", "Page.enable", "Page.addScriptToEvaluateOnNewDocument", "Page.startScreencast", "Page.captureScreenshot", "Page.screencastFrameAck", "jpeg-1",
        "Page.stopScreencast", "Page.getLayoutMetrics", "Page.startScreencast", "Page.captureScreenshot", "Page.screencastFrameAck", "jpeg-2",
    ]


@pytest.mark.asyncio
async def test_pane_screencast_reports_a_page_emulated_narrower_than_the_window_at_its_own_size(monkeypatch):
    # Chrome keeps a window at least about 500 px wide: a 419 px page fills the frame's left 419 px.
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    socket.layout = {"cssLayoutViewport": {"clientWidth": 419, "clientHeight": 653}}
    # A frame of the capture being replaced, sent before the measurement, may be at an old size.
    socket.incoming.put_nowait(_frame(1, 1280))
    running = asyncio.create_task(cast.run())
    await _started(log)
    for message in (_frame(2, 500), _frame(3, 400), None):
        socket.incoming.put_nowait(message)
    await asyncio.wait_for(running, 1)
    assert [entry[2] for entry in log if entry[1] == "Page.screencastFrameAck"] == [{"sessionId": 1}, {"sessionId": 2}, {"sessionId": 3}]
    assert [entry[1]["metadata"] for entry in log if entry[0] == "pane"] == [
        {"deviceWidth": 419, "deviceHeight": 600}, {"deviceWidth": 400, "deviceHeight": 600}]


async def _sent(log, method, count=1):
    while sum(entry[1] == method for entry in log if entry[0] == "cdp") < count:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_capture_starts_once_its_measurement_answers_so_a_still_pages_only_frame_is_shown_at_any_size(monkeypatch):
    # A still page's start sends its only frame. Chrome reports the whole view, which is wider than the
    # page's layout viewport by a classic scrollbar, so that frame is shown at the page's size, not dropped.
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    socket.layout = {"cssLayoutViewport": {"clientWidth": 400, "clientHeight": 600}}
    socket.measurements = []
    running = asyncio.create_task(cast.run())
    await _sent(log, "Page.addScriptToEvaluateOnNewDocument")
    for _ in range(10):
        await asyncio.sleep(0)
    assert not any(entry[1] == "Page.startScreencast" for entry in log)
    # A frame before the answer is the replaced capture's, at whatever size it had.
    socket.incoming.put_nowait(_frame(1, 1280))
    await _sent(log, "Page.screencastFrameAck")
    socket.incoming.put_nowait(socket.measurements.pop())
    await _started(log)
    for message in (_frame(2, 415), None):
        socket.incoming.put_nowait(message)
    await asyncio.wait_for(running, 1)
    assert [entry[1] for entry in log if entry[0] == "pane"] == [
        {"type": "frame", "data": "jpeg-2", "metadata": {"deviceWidth": 400, "deviceHeight": 600}}]


@pytest.mark.asyncio
async def test_restarts_before_their_measurements_answer_start_one_capture_after_the_last(monkeypatch):
    # Chrome refuses a second start while a capture runs; overlapping restarts must not send one.
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    socket.measurements = []
    running = asyncio.create_task(cast.run())
    await _sent(log, "Page.getLayoutMetrics")
    await cast.restart()
    await cast.restart()
    answers = list(socket.measurements)
    socket.measurements.clear()
    for answer in answers:
        socket.incoming.put_nowait(answer)
    await _started(log)
    for _ in range(10):
        await asyncio.sleep(0)
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)
    assert len(answers) == 3
    assert sum(entry[1] == "Page.startScreencast" for entry in log) == 1
    assert sum(entry[1] == "Page.captureScreenshot" for entry in log) == 1


@pytest.mark.asyncio
async def test_pane_screencast_restart_after_the_pane_left_does_not_fail_the_command(monkeypatch):
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    running = asyncio.create_task(cast.run())
    while not log:
        await asyncio.sleep(0)

    async def closed(_text):
        raise browser_screencast.websockets.ConnectionClosed(None, None)

    socket.send = closed
    await cast.restart()
    socket.incoming.put_nowait(None)
    await asyncio.wait_for(running, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    {"id": 1, "error": {"message": "Not allowed"}},
    {"method": "Page.screencastFrame", "params": {"data": "jpeg", "metadata": {}}},
    {"id": 1, "result": {}},
])
async def test_pane_screencast_ends_visibly_when_chrome_refuses_a_frame_cannot_be_acknowledged_or_the_page_has_no_size(monkeypatch, reply):
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)

    async def send(text):
        log.append(("cdp", json.loads(text)["method"]))

    socket.send = send
    socket.incoming.put_nowait(reply)
    with pytest.raises(browser_screencast.BrowserSessionError, match="This page couldn't be shown"):
        await asyncio.wait_for(cast.run(), 1)
    assert not any(entry[0] == "pane" for entry in log)
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_every_start_asks_chrome_to_draw_once_and_a_refused_draw_is_logged_not_fatal(monkeypatch, caplog, refused):
    """A still page can draw nothing after a start; one screenshot makes Chrome draw, and the screencast sends that."""
    log = []
    cast, socket, _connected = _screencast(monkeypatch, log)
    sent = socket.send

    async def send(text):
        message = json.loads(text)
        if refused and message["method"] == "Page.captureScreenshot":
            log.append(("cdp", message["method"], message["params"]))
            socket.incoming.put_nowait({"id": message["id"], "error": {"message": "Unable to capture screenshot"}})
        else:
            await sent(text)

    socket.send = send
    running = asyncio.create_task(cast.run())
    await _started(log)
    await cast.restart()
    await _sent(log, "Page.startScreencast", 2)
    for message in (_frame(1, 400), None):
        socket.incoming.put_nowait(message)
    await asyncio.wait_for(running, 1)
    # One draw per start, each right after it; the screenshot's own reply is never shown in the pane.
    methods = [entry[1] for entry in log if entry[0] == "cdp" and entry[1] in ("Page.startScreencast", "Page.captureScreenshot")]
    assert methods == ["Page.startScreencast", "Page.captureScreenshot"] * 2
    assert [entry[1]["data"] for entry in log if entry[0] == "pane"] == ["jpeg-1"]
    assert ("Chrome refused the draw" in caplog.text) is refused
