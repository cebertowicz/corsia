"""Wheel bursts must not hold later browser input behind every queued frame."""
import asyncio
import json

import pytest

from ws.browser_pane_input import pane_messages


def wheel(delta, ident, x=5):
    return {'type': 'input_mouse', 'eventType': 'mouseWheel', 'x': x, 'y': 6,
            'deltaX': 0, 'deltaY': delta, 'id': ident}


@pytest.mark.asyncio
async def test_wheels_coalesce_only_until_direction_or_other_input_changes():
    click = {'type': 'input_mouse', 'eventType': 'mousePressed', 'x': 5, 'y': 6}
    key = {'type': 'input_keyboard', 'eventType': 'keyDown', 'key': 'a'}
    sent = asyncio.Queue()
    for item in (wheel(10, 1), wheel(20, 2), wheel(-5, 3), wheel(-7, 4), click,
                 wheel(8, 5), key, wheel(9, 6), wheel(2, 7, x=7)):
        sent.put_nowait(json.dumps(item))

    class Pane:
        async def receive_text(self):
            return await sent.get()

    messages = pane_messages(Pane())
    assert [await anext(messages) for _ in range(7)] == [
        wheel(30, 2), wheel(-12, 4), click, wheel(8, 5), key, wheel(9, 6), wheel(2, 7, x=7)]
    await messages.aclose()


@pytest.mark.asyncio
async def test_wheels_arriving_while_delivery_waits_preserve_order():
    sent = asyncio.Queue()
    sent.put_nowait(json.dumps(wheel(1, 1)))

    class Pane:
        async def receive_text(self):
            return await sent.get()

    messages = pane_messages(Pane())
    assert await anext(messages) == wheel(1, 1)
    for item in (wheel(4, 2), wheel(5, 3), {'type': 'input_keyboard'}, wheel(6, 4)):
        sent.put_nowait(json.dumps(item))
    await asyncio.sleep(0)
    assert [await anext(messages) for _ in range(3)] == [wheel(9, 3), {'type': 'input_keyboard'}, wheel(6, 4)]
    await messages.aclose()


@pytest.mark.asyncio
async def test_invalid_wheel_is_not_swallowed_by_coalescing():
    sent = asyncio.Queue()
    invalid = {**wheel(1, 2), 'deltaY': True}
    for item in (wheel(1, 1), invalid, wheel(1, 3)):
        sent.put_nowait(json.dumps(item))

    class Pane:
        async def receive_text(self):
            return await sent.get()

    messages = pane_messages(Pane())
    assert [await anext(messages) for _ in range(3)] == [wheel(1, 1), invalid, wheel(1, 3)]
    await messages.aclose()
