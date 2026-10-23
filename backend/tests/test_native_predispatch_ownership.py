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
        SimpleNamespace(exact=selection, checkpoint_tail=None), None, None,
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


def _assert_stopped_before_warm_release(db, turn_id, runtime_task):
    status = db.execute('SELECT status FROM active_turns WHERE id=?', (turn_id,)).fetchone()[0]
    assert status != 'running'
    delivery = db.execute("SELECT delivery_state,write_outcome FROM messages WHERE id='warm-input'").fetchone()
    assert tuple(delivery) == ('cancelled', 'proved_no_write')
    assert runtime_task is not None and runtime_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption", [None, "user-stop", "runtime-boundary"],
    ids=["prefix-reuse", "user-stop", "runtime-boundary"],
)
async def test_native_warm_wait_has_one_turn_owner(
    isolated_db, monkeypatch, tmp_path, interruption,
):
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
    warm_cancelled, warm_joined = asyncio.Event(), asyncio.Event()
    writes = []
    cancelled_loop_payloads = []

    async def warm():
        try:
            await release_warm.wait()
        except asyncio.CancelledError:
            warm_cancelled.set()
            warm_joined.set()
            raise
        return {'ok': True}

    warm_task, _, _ = await thread_warm.start_thread_warm(
        'warm-thread', 'prefix', warm, model=target.catalog_id, admission_check=lambda: None)
    original_join = thread_warm.join_requested_warm_for_delivery

    async def join(*args, **kwargs):
        joined_warm.set()
        return await original_join(*args, **kwargs)

    async def prepare(batch, **kwargs):
        return PreparedBatch(thread, target, 'native', False, 'hello', None, [], None,
                             context, None, {}, batch=batch)

    async def provider(**kwargs):
        # The canonical Native loop still runs on a pre-set cancellation so it
        # emits its terminal payload and validates pending tool call IDs, but it
        # cannot make a provider write before its matching warm finishes.
        if kwargs['cancel_event'].is_set():
            payload = {'terminal_owner': 'native-loop'}
            cancelled_loop_payloads.append(payload)
            await kwargs['on_step']('turn_cancelled', payload)
            return
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
        if interruption == 'user-stop':
            # The user Stop returns after its bounded owner chance, so a stuck
            # pre-dispatch runtime fails this assertion without waiting 10s.
            from subagents import live_registry

            monkeypatch.setattr(live_registry, 'STOP_LOCK_TIMEOUT_S', 1.0)
            receipt = await do_stop(
                {'id': 'user-1'}, 'warm-thread', state._active_turns.get('warm-thread'),
                state._active_llm_tasks.get('warm-thread'), turn,
                owns_thread_fn=AsyncMock(return_value=True))
            assert receipt.outcome == 'stopped'
        elif interruption == 'runtime-boundary':
            from subagents import cc_runtime, codex_runtime
            from thread import runtime_boundary

            monkeypatch.setattr(runtime_boundary, '_DESTRUCTIVE_QUIESCE_TIMEOUT_S', 0.05)
            monkeypatch.setattr(cc_runtime, 'stop', AsyncMock(return_value=False))
            monkeypatch.setattr(codex_runtime, 'stop', AsyncMock(return_value=False))
            effects = await runtime_boundary.quiesce_exact_turn_owner(
                'warm-thread',
                expected_turn=state._active_turns['warm-thread'].turn_identity,
                user_id='user-1',
            )
            assert effects['native_cancelled'] is True
        if interruption is not None:
            assert cancelled_loop_payloads == [{'terminal_owner': 'native-loop'}]
            _assert_stopped_before_warm_release(db, turn, runtime_task)
            if interruption == 'user-stop':
                assert warm_cancelled.is_set()
                assert warm_joined.is_set()
                assert warm_task.done() and warm_task.cancelled()
                assert 'warm-thread' not in state._active_warm_tasks
            else:
                assert not warm_task.done()
                assert not warm_task.cancelled()
        release_warm.set()
        await asyncio.gather(task, return_exceptions=True)
        if runtime_task is not None:
            await asyncio.gather(runtime_task, return_exceptions=True)
        assert not [frame for frame in frames if frame['type'] == 'error'], frames
        delivery = db.execute("SELECT delivery_state,write_outcome FROM messages WHERE id='warm-input'").fetchone()
        cancelled = interruption is not None
        assert tuple(delivery) == (('cancelled', 'proved_no_write') if cancelled else ('delivered', 'accepted'))
        assert len(writes) == (0 if cancelled else 1)
    finally:
        release_warm.set()
        await asyncio.gather(task, warm_task, *([runtime_task] if runtime_task else []), return_exceptions=True)
        db.close()


@pytest.mark.asyncio
async def test_preset_cancellation_bypasses_warm_and_keeps_provider_loop_validation(monkeypatch):
    from core import native_turn_runtime, thread_warm

    cancel = asyncio.Event()
    cancel.set()
    join = AsyncMock(side_effect=AssertionError('a cancelled turn must not wait for warm'))
    runner = AsyncMock()
    monkeypatch.setattr(thread_warm, 'join_requested_warm_for_delivery', join)
    monkeypatch.setattr(native_turn_runtime, 'run_turn_with_steps', runner)
    turn = native_turn_runtime.NativeTurn(
        agent=object(), thread_id='cancelled-thread', content='', model='test/model',
        turn_provider={}, handler=object(), cancel_event=cancel, identity=object(),
    )

    assert await native_turn_runtime._run_native_turn(turn, {}) == 'cancelled'
    join.assert_not_awaited()
    runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_await_thread_warm_distinguishes_warm_and_waiter_cancellation(monkeypatch):
    """A replacement can cancel its warm; cancelling delivery cannot."""
    from core import thread_warm
    from infra import state

    shielded = asyncio.Event()
    original_shield = thread_warm.asyncio.shield

    def shield(awaitable):
        shielded.set()
        return original_shield(awaitable)

    monkeypatch.setattr(thread_warm.asyncio, 'shield', shield)
    state._active_warm_tasks.clear()
    try:
        release = asyncio.Event()
        warm = asyncio.create_task(release.wait())
        state._active_warm_tasks['replacement'] = warm
        waiter = asyncio.create_task(thread_warm.await_thread_warm('replacement'))
        await shielded.wait()
        warm.cancel()
        assert await waiter is False

        shielded.clear()
        release = asyncio.Event()
        warm = asyncio.create_task(release.wait())
        state._active_warm_tasks['delivery'] = warm
        waiter = asyncio.create_task(thread_warm.await_thread_warm('delivery'))
        await shielded.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not warm.cancelled()
        release.set()
        await warm
    finally:
        for task in tuple(state._active_warm_tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*state._active_warm_tasks.values(), return_exceptions=True)
        state._active_warm_tasks.clear()
