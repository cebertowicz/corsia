"""Saved steps and reconnects use the same server-owned answer placement."""

import asyncio

from core.step_handler import StepHandler
from core.turn_core import TurnIdentity
from infra.database import get_db
from infra.state import turn_buffers
from ws.turn_helpers import _update_turn_buffer
from test_chat_comment_answers import _seed


def test_cc_new_turn_drops_stopped_turns_held_delta():
    from subagents.cc_accept import _bind_turn
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_event_simple import on_text_delta
    from subagents.cc_runtime_state import _Live

    old = TurnIdentity('old', 1)
    new = TurnIdentity('new', 2)
    live = _Live(session=None, parent_thread_id=None, name='cc', corsia_turn=old)
    frames = []
    ctx = CCEventContext(live, 'thread', lambda _thread, frame: frames.append(frame), None)

    async def run():
        await on_text_delta(ctx, {'text': 'Intro\n\n[c1', '_corsia_turn_identity': old})
        assert frames[-1]['content'] == 'Intro'
        _bind_turn(live, None, new)  # The previous turn stopped without saving its partial tag.
        await on_text_delta(ctx, {'text': 'New turn', '_corsia_turn_identity': new})
        assert frames[-1]['content'] == 'New turn'

    asyncio.run(run())


def test_codex_new_turn_drops_stopped_turns_held_delta(isolated_db):
    from types import SimpleNamespace
    from subagents import codex_runtime
    from subagents.codex_runtime_events import _on_text_delta
    from test_thread_root_visibility import _seed_threads

    _seed_threads([('cx', None)], turns=[(2, 'new', 'cx')])
    old = TurnIdentity('old', 1)
    new = TurnIdentity('new', 2)
    live = codex_runtime._LiveCodex(session=None, parent_thread_id=None, name='cx', corsia_turn=old)
    frames = []
    ctx = SimpleNamespace(live=live, thread_id='cx', bcast=lambda _thread, frame: frames.append(frame))

    async def run():
        await _on_text_delta(ctx, {'text': 'Intro\n\n[c1', '_corsia_turn_identity': old})
        assert frames[-1]['content'] == 'Intro'
        # The previous turn stopped without saving its partial tag.
        await codex_runtime._start_live_turn('cx', live, 'input', successor_turn_id='new',
                                             bcast=lambda _thread, frame: None, preadmitted_turn=new)
        await _on_text_delta(ctx, {'text': 'New turn', '_corsia_turn_identity': new})
        assert frames[-1]['content'] == 'New turn'

    asyncio.run(run())


def test_native_stream_reconnect_and_saved_projection(isolated_db):
    db = get_db()
    try:
        _seed(db)
        # With Focus off every assistant step is shown, so its answers are placed as it is saved.
        from infra.settings import set_setting

        set_setting("features", {"focus_mode": False}, db=db)
        db.commit()
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


def test_cli_emitters_project_saved_step_and_clear_held_text(isolated_db, monkeypatch):
    from types import SimpleNamespace
    from subagents import cc_event_steps, codex_runtime_events
    from subagents.assistant_step_display import load_assistant_step_display
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_event_simple import on_text_delta
    from subagents.cc_runtime_state import _Live
    from test_chat_comment_answers import _answer

    db = get_db()
    try:
        _seed(db)
        _answer(db, 'Intro\n\n[c1.1] Private')
        db.commit()
        identity = TurnIdentity('turn', 1)
        event = {'_corsia_turn_identity': identity, 'text': 'Intro\n\n[c1.1] Private'}
        for kind in ('cc', 'codex'):
            live = _Live(session=None, parent_thread_id=None, name=kind, corsia_turn=identity)
            frames = []
            broadcast = lambda _thread, frame: frames.append(frame)
            if kind == 'cc':
                ctx = CCEventContext(live, 'thread', broadcast, db)
                monkeypatch.setattr(cc_event_steps, 'persist_assistant_step', lambda *a, **kw: 'answer')
                delta = on_text_delta
                save = cc_event_steps.on_assistant_step
            else:
                live.thinking_provenance = None
                ctx = SimpleNamespace(live=live, thread_id='thread', bcast=broadcast,
                                      persist_assistant_text=lambda *a, **kw: 'answer',
                                      load_assistant_step_display=load_assistant_step_display)
                async def run_db(fn, *args, **kwargs):
                    return fn(*args, **kwargs)
                monkeypatch.setattr(codex_runtime_events, 'run_in_db', run_db)
                delta = codex_runtime_events._on_text_delta
                save = codex_runtime_events._on_assistant_text

            async def run():
                await delta(ctx, {'_corsia_turn_identity': identity, 'text': 'Intro\n\n[c1'})
                assert frames[-1]['content'] == 'Intro'
                await save(ctx, event)
                assert frames[-1]['chat_answer_cids'] == ['c1.1']
                assert frames[-1]['chat_visible_content'] == 'Intro'
                await delta(ctx, {'_corsia_turn_identity': identity, 'text': 'Next'})
                assert frames[-1]['content'] == 'Next'

            asyncio.run(run())
    finally:
        db.close()
