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
