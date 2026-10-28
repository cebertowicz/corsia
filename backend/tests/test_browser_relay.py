"""Exercise input and reliable results while navigation is still running."""
import asyncio
import contextlib
import json

import pytest
from fastapi import WebSocketDisconnect
from websockets.exceptions import WebSocketException

from tests.test_browser_pane import FakePane, screencast  # noqa: F401 - autouse fixture: no real page screencast
from ws import browser_stream


@pytest.mark.asyncio
async def test_navigation_does_not_block_input_and_its_result_is_delivered(monkeypatch):
    entered, input_received, navigation_delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def open_url(*_args):
        entered.set()
        await input_received.wait()
        return 'ok'

    monkeypatch.setattr(browser_stream.browser_pane, 'open_url', open_url)
    async def page_info(_thread_id):
        return 'https://example.test', 'Example'
    monkeypatch.setattr(browser_stream.browser_pane, 'page_info', page_info)

    class Pane(FakePane):
        async def receive_text(self):
            if len(self.messages) == 1:
                await entered.wait()
            if self.messages:
                return self.messages.pop(0)
            await navigation_delivered.wait()
            raise WebSocketDisconnect()

        async def send_text(self, payload):
            await super().send_text(payload)
            if json.loads(payload).get('type') == 'navigate_result':
                navigation_delivered.set()

    async def key_event(_thread_id, payload, _connection_token):
        assert payload == {'type': 'input_keyboard', 'key': 'a'}
        input_received.set()

    monkeypatch.setattr(browser_stream.browser_pane, 'key_event', key_event)

    pane = Pane([json.dumps(message) for message in [
        {'type': 'take_over'}, {'type': 'navigate', 'url': 'https://example.test'},
        {'type': 'input_keyboard', 'key': 'a'},
    ]])
    try:
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(browser_stream._relay(pane, 'thread', 'user'), 1)
        assert input_received.is_set()
        assert any(result.get('type') == 'navigate_result' and result.get('error') is None for result in (json.loads(message) for message in pane.text))
        assert browser_stream._runners == {}
    finally:
        browser_stream._runners.clear()


@pytest.mark.asyncio
async def test_refused_pane_fit_is_sent_to_the_connected_pane(monkeypatch):
    from host_access.browser_session_state import BrowserSessionError
    delivered = asyncio.Event()
    async def refuse(_thread, _appearance):
        raise BrowserSessionError('refused')
    monkeypatch.setattr(browser_stream.browser_pane, 'match_pane', refuse)
    class Pane(FakePane):
        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            await delivered.wait()
            raise WebSocketDisconnect()
        async def send_text(self, payload):
            await super().send_text(payload)
            if json.loads(payload).get('type') == 'pane_error':
                delivered.set()
    pane = Pane([json.dumps({'type': 'appearance', 'width': 700, 'height': 900, 'scale': 1, 'scheme': 'dark'})])
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(browser_stream._relay(pane, 'fit-thread', 'user'), 1)
    assert any(json.loads(item).get('error') == "CORSIA couldn’t fit the page to the pane. Try resizing the pane again."
               for item in pane.text)


@pytest.mark.asyncio
async def test_clipboard_actions_stay_on_the_connected_page(monkeypatch):
    actions = []
    delivered = asyncio.Event()
    async def selection(thread, connection, request_id):
        actions.append(('selection', thread, connection))
        return 'chosen'
    async def paste(thread, connection, text):
        actions.append(('paste', thread, connection, text))
    async def cut(thread, connection, text, request_id):
        actions.append(('cut', thread, connection, text))
        delivered.set()
    monkeypatch.setattr(browser_stream.browser_pane, 'clipboard_selection', selection)
    monkeypatch.setattr(browser_stream.browser_pane, 'clipboard_paste', paste)
    monkeypatch.setattr(browser_stream.browser_pane, 'clipboard_cut', cut)
    class Pane(FakePane):
        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            await delivered.wait()
            raise WebSocketDisconnect()
    pane = Pane([json.dumps(message) for message in [
        {'type': 'clipboard_selection', 'id': 1},
        {'type': 'clipboard_paste', 'text': 'host'},
        {'type': 'clipboard_cut', 'id': 1, 'text': 'chosen'},
    ]])
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(browser_stream._relay(pane, 'thread', 'user'), 1)
    assert [action[0] for action in actions] == ['selection', 'paste', 'cut']
    assert len({action[2] for action in actions}) == 1
    assert actions[1][3] == 'host' and actions[2][3] == 'chosen'


@pytest.mark.asyncio
async def test_clipboard_failure_suggests_a_next_step(monkeypatch):
    from host_access.browser_session_state import BrowserSessionError
    from ws import browser_clipboard
    async def refuse(*_args):
        raise BrowserSessionError('refused')
    monkeypatch.setattr(browser_clipboard.browser_pane, 'clipboard_selection', refuse)
    monkeypatch.setattr(browser_clipboard.browser_pane, 'clipboard_cut', refuse)
    monkeypatch.setattr(browser_clipboard.browser_pane, 'clipboard_paste', refuse)
    messages = []
    for data in ({'type': 'clipboard_selection', 'id': 1},
                 {'type': 'clipboard_cut', 'id': 1, 'text': 'text'}, {'type': 'clipboard_paste', 'text': 'text'}):
        await browser_clipboard.handle('thread', 'pane', data, messages.append)
    assert [message['error'] for message in messages] == [
        "CORSIA couldn’t copy from the page. Select the text again and copy.",
        "CORSIA couldn’t cut the text. Select it again and cut.",
        "CORSIA couldn’t paste into the page. Try pasting again.",
    ]


@pytest.mark.asyncio
async def test_copy_inside_an_embedded_frame_says_the_clipboard_was_kept(monkeypatch):
    from host_access import browser_clipboard as page_clipboard
    from ws import browser_clipboard
    async def evaluate(*_args, **_kwargs):
        return {'result': {'type': 'object', 'subtype': 'null', 'value': None}}
    class Cdp:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        call = staticmethod(evaluate)
    monkeypatch.setattr(page_clipboard, 'CdpClient', lambda _endpoint: Cdp())
    async def selection(_thread, _connection, request_id):
        return await page_clipboard.clipboard_selection('endpoint', f'pane:{request_id}')
    monkeypatch.setattr(browser_clipboard.browser_pane, 'clipboard_selection', selection)
    messages = []
    await browser_clipboard.handle('thread', 'pane', {'type': 'clipboard_selection', 'id': 1}, messages.append)
    assert messages == [{'type': 'clipboard_selection', 'id': 1,
                         'error': "CORSIA can’t copy from inside this embedded area yet. Your clipboard wasn’t changed."}]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [OSError('CDP disconnected'), WebSocketException('message too big')])
async def test_clipboard_transport_failure_reports_error_and_allows_retry(monkeypatch, failure):
    from ws import browser_clipboard
    calls = 0
    async def selection(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return 'recovered'
    monkeypatch.setattr(browser_clipboard.browser_pane, 'clipboard_selection', selection)
    messages = []
    await browser_clipboard.handle('thread', 'pane', {'type': 'clipboard_selection', 'id': 1}, messages.append)
    await browser_clipboard.handle('thread', 'pane', {'type': 'clipboard_selection', 'id': 2}, messages.append)
    assert messages == [
        {'type': 'clipboard_selection', 'id': 1, 'error': 'CORSIA couldn’t copy from the page. Select the text again and copy.'},
        {'type': 'clipboard_selection', 'id': 2, 'text': 'recovered'},
    ]


@pytest.mark.asyncio
async def test_one_runner_keeps_one_waiting_navigation_and_the_newest_appearance(monkeypatch):
    first_started, third_started = asyncio.Event(), asyncio.Event()
    release_first, release_third, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    commands, results = [], []

    async def open_url(_thread_id, url, _connection_token):
        commands.append(url)
        if url.endswith("first"):
            first_started.set()
            await release_first.wait()
            return "Error:first"
        third_started.set()
        await release_third.wait()
        return "Error:third"

    async def match_pane(_thread_id, appearance):
        commands.append(appearance.width)

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)
    monkeypatch.setattr(browser_stream.browser_pane, "match_pane", match_pane)

    class Pane(FakePane):
        async def receive_text(self):
            if len(self.messages) == 3:
                await first_started.wait()
            if self.messages:
                return self.messages.pop(0)
            await delivered.wait()
            raise WebSocketDisconnect()

        async def send_text(self, payload):
            await super().send_text(payload)
            result = json.loads(payload)
            if result.get("type") == "navigate_result":
                results.append(result)
                if len(results) == 3:
                    delivered.set()

    pane = Pane([json.dumps(message) for message in [
        {"type": "take_over"},
        {"type": "navigate", "url": "https://example.test/first"},
        {"type": "appearance", "width": 400, "height": 600, "scale": 1, "scheme": "light"},
        {"type": "navigate", "url": "https://example.test/second"},
        {"type": "navigate", "url": "https://example.test/third"},
    ]])
    relay = asyncio.create_task(browser_stream._relay(pane, "thread", "user"))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        release_first.set()
        await asyncio.wait_for(third_started.wait(), 1)
        # Nothing else ran beside the running navigation, and the replaced one never started.
        assert commands == ["https://example.test/first", "https://example.test/third"]
        release_third.set()
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(relay, 1)
        assert results == [
            {"type": "navigate_result", "error": "Superseded by a newer navigation"},
            {"type": "navigate_result", "error": "Error:first"},
            {"type": "navigate_result", "error": "Error:third"},
        ]
        assert commands[-1] == 400
    finally:
        if not relay.done():
            relay.cancel()
        browser_stream._runners.clear()


@pytest.mark.asyncio
async def test_relay_exit_keeps_running_navigation_alive(monkeypatch):
    started, release, finished, cancelled = (asyncio.Event() for _ in range(4))

    async def open_url(*_args):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()
        return "ok"

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)
    async def page_info(_thread_id):
        return "https://example.test", "Example"
    monkeypatch.setattr(browser_stream.browser_pane, "page_info", page_info)

    class Pane(FakePane):
        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            await started.wait()
            raise WebSocketDisconnect()

    pane = Pane([json.dumps(message) for message in [
        {"type": "take_over"},
        {"type": "navigate", "url": "https://example.test"},
    ]])
    try:
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(browser_stream._relay(pane, "thread", "user"), 1)
        navigation = browser_stream._runners["thread"]
        assert not cancelled.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.wait_for(navigation, 1)
        assert not cancelled.is_set()
    finally:
        browser_stream._runners.clear()


@pytest.mark.asyncio
async def test_a_slow_pointer_move_lets_later_moves_collapse_but_never_a_click(monkeypatch):
    def mouse(kind, x):
        return {"type": "input_mouse", "eventType": kind, "x": x, "y": 1, "button": "none" if kind == "mouseMoved" else "left"}

    sent = [mouse("mouseMoved", 1), mouse("mouseMoved", 2), mouse("mouseMoved", 3), mouse("mousePressed", 3),
            mouse("mouseReleased", 3), mouse("mouseMoved", 4), mouse("mouseMoved", 5)]
    moving, read_all = asyncio.Event(), asyncio.Event()

    class Pane(FakePane):
        async def receive_text(self):
            if len(self.messages) < len(sent):
                await moving.wait()
            if self.messages:
                return self.messages.pop(0)
            read_all.set()
            raise WebSocketDisconnect()

    delivered = []

    async def mouse_event(_thread_id, event, _connection_token):
        # Chrome takes a move only after its next frame; the pane keeps sending meanwhile.
        moving.set()
        if not delivered:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(read_all.wait(), 0.5)
        delivered.append((event["eventType"], event["x"]))

    monkeypatch.setattr(browser_stream.browser_pane, "mouse_event", mouse_event)
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(browser_stream._relay(Pane([json.dumps(event) for event in sent]), "thread", "user"), 2)
    # Only moves another move replaced are dropped; the click and the final position all arrive, in order.
    assert delivered == [("mouseMoved", 1), ("mouseMoved", 3), ("mousePressed", 3), ("mouseReleased", 3), ("mouseMoved", 5)]


@pytest.mark.asyncio
async def test_navigation_result_overflow_closes_stalled_pane(monkeypatch, caplog):
    monkeypatch.setattr(browser_stream.PaneFrames, "_MAX_RELIABLE", 1)

    async def open_url(*_args):
        return "ok"

    async def watch_page_metadata(*args):
        report = args[-1]
        await report({"type": "browser_metadata", "url": "https://example.test", "title": "Example"})
        await report({"type": "browser_metadata", "url": "https://example.test/next", "title": "Next"})
        await asyncio.Future()

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)
    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", watch_page_metadata)

    class Pane(FakePane):
        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            await asyncio.Future()

        async def send_text(self, payload):
            self.text.append(payload)
            await asyncio.Future()

    pane = Pane([json.dumps(message) for message in [
        {"type": "take_over"},
        {"type": "navigate", "url": "https://example.test"},
    ]])
    try:
        await asyncio.wait_for(browser_stream._relay(pane, "thread", "user"), 1)
        assert pane.closed == [1013]
        assert "browser pane navigation failed" not in caplog.text
    finally:
        browser_stream._runners.clear()


@pytest.mark.asyncio
async def test_a_lost_address_watch_reconnects_then_ends_the_stream_visibly(monkeypatch, caplog):
    monkeypatch.setattr(browser_stream, "PANE_RETRY_DELAYS", (0,))
    attempts = []

    async def watch_page_metadata(_thread_id, _connection_token, report):
        attempts.append(len(attempts))
        if len(attempts) == 1:
            # An initial metadata report is not a stable run, so its quick drop spends retry budget.
            await report({"type": "browser_metadata", "url": "https://example.test", "domain": "example.test", "title": "Example"})
            raise OSError("connection reset")
        raise browser_stream.BrowserSessionError("This page couldn't be checked")

    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", watch_page_metadata)

    class Pane(FakePane):
        async def receive_text(self):
            await asyncio.Future()

    pane = Pane()
    with pytest.raises(browser_stream.BrowserSessionError, match="This page couldn't be checked"):
        await asyncio.wait_for(browser_stream._relay(pane, "thread", "user"), 1)
    assert attempts == [0, 1]
    assert caplog.text.count("lost its page address watch") == 2
    assert {"type": "browser_metadata", "url": "https://example.test", "domain": "example.test",
            "title": "Example"} in [json.loads(message) for message in pane.text]


@pytest.mark.asyncio
async def test_a_lost_screencast_reconnects_without_ending_the_pane(monkeypatch):
    monkeypatch.setattr(browser_stream, "PANE_RETRY_DELAYS", (0, 0, 0))
    attempts, reconnected = [], asyncio.Event()

    async def screencast(*_args):
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise browser_stream.BrowserSessionError("page target moved")
        reconnected.set()
        await asyncio.Future()

    async def watch_page_metadata(*_args):
        await asyncio.Future()

    class Pane(FakePane):
        async def receive_text(self):
            await asyncio.Future()

    monkeypatch.setattr(browser_stream.browser_pane, "screencast", screencast)
    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", watch_page_metadata)
    relay = asyncio.create_task(browser_stream._relay(Pane(), "thread", "user"))
    try:
        await asyncio.wait_for(reconnected.wait(), 1)
        assert attempts == [0, 1]
        assert not relay.done()
    finally:
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)


@pytest.mark.asyncio
async def test_screencast_clean_returns_spend_the_retry_budget(monkeypatch):
    monkeypatch.setattr(browser_stream, "PANE_RETRY_DELAYS", (0,))
    attempts = []

    async def screencast(*_args):
        attempts.append(len(attempts))

    async def watch_page_metadata(*_args):
        await asyncio.Future()

    class Pane(FakePane):
        async def receive_text(self):
            await asyncio.Future()

    monkeypatch.setattr(browser_stream.browser_pane, "screencast", screencast)
    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", watch_page_metadata)
    with pytest.raises(browser_stream.BrowserSessionError, match="stopped showing the page"):
        await asyncio.wait_for(browser_stream._relay(Pane(), "thread", "user"), 1)
    assert attempts == [0, 1]


@pytest.mark.asyncio
async def test_screencast_frame_between_quick_drops_does_not_reset_retry_budget(monkeypatch):
    monkeypatch.setattr(browser_stream, "PANE_RETRY_DELAYS", (0,))
    attempts = []

    async def screencast(_thread_id, _connection_token, deliver):
        attempts.append(len(attempts))
        if len(attempts) == 2:
            deliver("frame")
        raise OSError("connection reset")

    async def watch_page_metadata(*_args):
        await asyncio.Future()

    class Pane(FakePane):
        async def receive_text(self):
            await asyncio.Future()

    monkeypatch.setattr(browser_stream.browser_pane, "screencast", screencast)
    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", watch_page_metadata)
    with pytest.raises(browser_stream.BrowserSessionError, match="stopped showing the page"):
        await asyncio.wait_for(browser_stream._relay(Pane(), "thread", "user"), 1)
    assert attempts == [0, 1]


@pytest.mark.asyncio
async def test_metadata_report_between_quick_drops_does_not_reset_retry_budget(monkeypatch):
    monkeypatch.setattr(browser_stream, "PANE_RETRY_DELAYS", (0,))
    attempts = []

    async def screencast(*_args):
        await asyncio.Future()

    async def watch_page_metadata(_thread_id, _connection_token, report):
        attempts.append(len(attempts))
        if len(attempts) == 2:
            await report({"type": "browser_metadata", "url": "https://example.test", "title": "Example"})
        raise OSError("connection reset")

    class Pane(FakePane):
        async def receive_text(self):
            await asyncio.Future()

    monkeypatch.setattr(browser_stream.browser_pane, "screencast", screencast)
    monkeypatch.setattr(browser_stream.browser_pane, "watch_page_metadata", watch_page_metadata)
    with pytest.raises(browser_stream.BrowserSessionError, match="stopped following the page's address"):
        await asyncio.wait_for(browser_stream._relay(Pane(), "thread", "user"), 1)
    assert attempts == [0, 1]
