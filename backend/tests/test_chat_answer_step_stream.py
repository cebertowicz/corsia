"""Saved steps and reconnects use the same server-owned answer placement."""

import asyncio

from core.step_handler import StepHandler
from core.turn_core import TurnIdentity
from infra.database import get_db
from infra.state import turn_buffers
from ws.turn_helpers import _update_turn_buffer
from test_chat_comment_answers import _seed


def test_native_stream_reconnect_and_saved_projection(isolated_db):
    db = get_db()
    try:
        _seed(db)
    finally:
        db.close()
    identity = TurnIdentity('turn-test', 1)
    events = []
    turn_buffers.init('thread', identity)

    async def broadcast(frame):
        _update_turn_buffer('thread', identity, frame)
        events.append(frame)

    handler = StepHandler('thread', {'model': 'test'}, broadcast, 'turn-test')
    handler.bind_turn_identity(identity)

    async def run():
        for chunk in ['Intro\n', '\n[c1', '.1] secret answer']:
            await handler.handle('text_delta', {'content': chunk})
        assert turn_buffers.snapshot('thread', identity)['content'] == 'Intro'
        await handler.handle('text', {'content': 'Intro\n\n[c1.1] secret answer'})
        assert events[-1]['chat_visible_content'] == 'Intro'
        assert events[-1]['chat_answer_cids'] == ['c1.1']
        for chunk in ['Ordinary\n\n', '[c1']:
            await handler.handle('text_delta', {'content': chunk})
        assert turn_buffers.snapshot('thread', identity)['content'] == 'Ordinary'
        await handler.handle('text', {'content': 'Ordinary\n\n[c1'})
        assert events[-1]['content'] == 'Ordinary\n\n[c1'
        assert 'chat_visible_content' not in events[-1]

    try:
        asyncio.run(run())
    finally:
        turn_buffers.clear('thread', identity)
