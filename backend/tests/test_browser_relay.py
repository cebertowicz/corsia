"""Exercise input and reliable results while navigation is still running."""
import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

from host_access import browser_control
from tests.test_browser_pane import FakePane, FakeStream
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

    class Stream(FakeStream):
        async def send(self, payload):
            await super().send(payload)
            if json.loads(payload).get('type') == 'input_keyboard':
                input_received.set()

    pane = Pane([json.dumps(message) for message in [
        {'type': 'take_over'}, {'type': 'navigate', 'url': 'https://example.test'},
        {'type': 'input_keyboard', 'key': 'a'},
    ]])
    browser_control._control_holders.clear()
    try:
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(browser_stream._relay(pane, Stream(), 'thread', 'user'), 1)
        assert input_received.is_set()
        assert {'type': 'navigate_result', 'error': None} in [json.loads(message) for message in pane.text]
        assert not browser_control.holds_control('thread', 'pane')
    finally:
        browser_control._control_holders.clear()


@pytest.mark.asyncio
async def test_navigation_requests_serialize_and_deliver_results_in_order(monkeypatch):
    first_started, second_started = asyncio.Event(), asyncio.Event()
    release_first, release_second, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started, results = [], []

    async def open_url(_thread_id, url, _connection_token):
        started.append(url)
        if url.endswith("first"):
            first_started.set()
            await release_first.wait()
            return "Error:first"
        second_started.set()
        await release_second.wait()
        return "Error:second"

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)

    class Pane(FakePane):
        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            await delivered.wait()
            raise WebSocketDisconnect()

        async def send_text(self, payload):
            await super().send_text(payload)
            result = json.loads(payload)
            if result.get("type") == "navigate_result":
                results.append(result)
                if len(results) == 2:
                    delivered.set()

    pane = Pane([json.dumps(message) for message in [
        {"type": "take_over"},
        {"type": "navigate", "url": "https://example.test/first"},
        {"type": "navigate", "url": "https://example.test/second"},
    ]])
    relay = asyncio.create_task(browser_stream._relay(pane, FakeStream(), "thread", "user"))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        assert started == ["https://example.test/first"]
        release_first.set()
        await asyncio.wait_for(second_started.wait(), 1)
        assert started == ["https://example.test/first", "https://example.test/second"]
        release_second.set()
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(relay, 1)
        assert results == [
            {"type": "navigate_result", "error": "Error:first"},
            {"type": "navigate_result", "error": "Error:second"},
        ]
    finally:
        if not relay.done():
            relay.cancel()
        browser_control._control_holders.clear()


@pytest.mark.asyncio
async def test_relay_exit_cancels_running_navigation(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def open_url(*_args):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

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
            await asyncio.wait_for(browser_stream._relay(pane, FakeStream(), "thread", "user"), 1)
        assert cancelled.is_set()
    finally:
        browser_control._control_holders.clear()


@pytest.mark.asyncio
async def test_navigation_result_overflow_closes_stalled_pane(monkeypatch, caplog):
    monkeypatch.setattr(browser_stream.PaneFrames, "_MAX_RELIABLE", 1)

    async def open_url(*_args):
        return "ok"

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)

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
        await asyncio.wait_for(browser_stream._relay(pane, FakeStream(), "thread", "user"), 1)
        assert pane.closed == [1013]
        assert "browser pane navigation failed" not in caplog.text
    finally:
        browser_control._control_holders.clear()
