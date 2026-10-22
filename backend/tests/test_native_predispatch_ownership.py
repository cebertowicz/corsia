"""Native admission remains owned while an explicit cache warm is in flight."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def _render_matching_prefix(monkeypatch, tmp_path, target):
    from api import thread_cache_warm
    from cache_warm_fixtures import make_exact_replay_selection
    from core.context_builder import ContextBuilder
    from core.turn_context import prepare_native_context_payload
    from native_checkpoint_fixtures import install_prefix_source_isolation
    from tools.registry import ToolRegistry

    agent = SimpleNamespace(
        tools=ToolRegistry(), _connect_mcp=AsyncMock(), max_tokens=8192,
        context=ContextBuilder(tmp_path),
    )
    monkeypatch.setattr('core.agent.get_agent', AsyncMock(return_value=agent))
    install_prefix_source_isolation(monkeypatch)
    selection = make_exact_replay_selection('warm-thread', [
        [('user', 'earlier request'), ('assistant', 'earlier answer')],
    ])
    monkeypatch.setattr(thread_cache_warm, '_warm_replay_candidates', lambda *_args: (
        SimpleNamespace(exact=selection, checkpoint_tail=None), None,
    ))
    warm_messages, warm_tools, _ = await thread_cache_warm._build_warm_prefix(
        'warm-thread', None, target, None,
    )
    context = await prepare_native_context_payload(
        thread_id='warm-thread', user_content='hello', target=target,
        project_id=None, media=None, replay_selection=selection,
    )
    assert json.dumps(context.messages[:-1]).encode() == json.dumps(warm_messages).encode()
    assert json.dumps(context.tool_definitions).encode() == json.dumps(warm_tools).encode()
    return context


def _capture_frames(monkeypatch, target):
    from infra import state

    frames = []

    def send(_ws, frame):
        frames.append(frame)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr(state, 'send_to_client', send)
    monkeypatch.setattr('ws.ingress.send_to_client', send)
    for name in ('broadcast_event', 'broadcast_to_user', 'broadcast_to_thread'):
        monkeypatch.setattr(state, name, lambda *_args: frames.append(_args[-1]))
    monkeypatch.setattr(state, 'owner_broadcast', lambda *_args: lambda frame: frames.append(frame))
    monkeypatch.setattr('infra.send_effects.run_batch_accepted_effects', AsyncMock())
    monkeypatch.setattr('ws.turn.validate_native_target', AsyncMock())
    monkeypatch.setattr('ws.turn.make_provider_params', lambda *_args: {'model': target.transport_model})
    monkeypatch.setattr('ws.turn.run_post_turn_tasks', AsyncMock())
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', [False, True], ids=['prefix-reuse', 'stop'])
async def test_native_warm_wait_has_one_turn_owner(isolated_db, monkeypatch, tmp_path, stop):
    """No provider write precedes the warm; Stop cancels without a send error."""
    from core import native_turn_runtime, thread_warm
    from infra import state, thread_execution as execution
    from infra.database import get_db
    from infra.input_render import PreparedBatch
    from infra.send_service import send_thread_input
    from llm.resolution import resolve_execution_target
    from ws.message_dispatch import _send_native_input
    from ws.stop_control import do_stop

    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) "
               "VALUES ('warm-thread','user-1','anthropic/claude-3-5-sonnet','t0','t0')")
    db.commit()
    thread = dict(db.execute("SELECT * FROM threads WHERE id='warm-thread'").fetchone())
    target = resolve_execution_target(thread['model'])
    context = await _render_matching_prefix(monkeypatch, tmp_path, target)
    frames = _capture_frames(monkeypatch, target)
    release_warm, joined_warm = asyncio.Event(), asyncio.Event()
    writes = []

    async def warm():
        await release_warm.wait()
        return {'ok': True}

    warm_task, _, _ = await thread_warm.start_thread_warm(
        'warm-thread', 'prefix', warm, model=target.catalog_id, admission_check=lambda: None)
    original_join = thread_warm.join_requested_warm_for_delivery

    async def join(*args):
        joined_warm.set()
        return await original_join(*args)

    async def prepare(batch, **kwargs):
        return PreparedBatch(thread, target, 'native', False, 'hello', None, [], None,
                             context, None, {}, batch=batch)

    async def provider(**kwargs):
        assert warm_task.done()
        assert kwargs['prepared_context'].messages == context.messages
        writes.append(kwargs['prepared_context'].messages)
        await kwargs['on_provider_response']()
        await kwargs['on_step']('turn_complete', {})

    monkeypatch.setattr(execution, 'persist_prepared_installed_context', lambda *_args: None)
    monkeypatch.setattr(execution, 'record_dispatch_manifest', lambda *_args: None)
    monkeypatch.setattr(thread_warm, 'join_requested_warm_for_delivery', join)
    monkeypatch.setattr(execution, '_prepare_batch', prepare)
    monkeypatch.setattr(native_turn_runtime, 'run_turn_with_steps', provider)
    task = asyncio.create_task(_send_native_input(
        object(), {'id': 'user-1'}, 'warm-thread', 'warm-input', 'hello', [], thread, {},
        run_in_db_fn=AsyncMock(), touch_thread_fn=lambda *_args: None,
        send_thread_input_fn=send_thread_input, release_admission=lambda: None))
    runtime_task = None
    try:
        await asyncio.wait_for(joined_warm.wait(), 10)
        assert writes == []
        assert any(frame['type'] == 'turn_activity' for frame in frames), frames
        turn = db.execute("SELECT id FROM active_turns WHERE thread_id='warm-thread' AND status='running'").fetchone()[0]
        runtime_task = state._active_turn_tasks.get('warm-thread')
        if stop:
            receipt = await do_stop(
                {'id': 'user-1'}, 'warm-thread', state._active_turns.get('warm-thread'),
                state._active_llm_tasks.get('warm-thread'), turn,
                owns_thread_fn=AsyncMock(return_value=True))
            assert receipt.outcome == 'stopped'
        release_warm.set()
        await asyncio.gather(task, return_exceptions=True)
        if runtime_task is not None:
            await asyncio.gather(runtime_task, return_exceptions=True)
        assert not [frame for frame in frames if frame['type'] == 'error'], frames
        delivery = db.execute("SELECT delivery_state,write_outcome FROM messages WHERE id='warm-input'").fetchone()
        assert tuple(delivery) == (('cancelled', 'proved_no_write') if stop else ('delivered', 'accepted'))
        assert len(writes) == (0 if stop else 1)
    finally:
        release_warm.set()
        await asyncio.gather(task, warm_task, *([runtime_task] if runtime_task else []), return_exceptions=True)
        db.close()
