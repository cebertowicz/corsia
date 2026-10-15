"""Exercise input and reliable results while navigation is still running."""
import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

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

    async def match_pane(_thread_id, appearance, _connection_token):
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
