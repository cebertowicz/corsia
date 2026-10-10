"""The real Native loop waits, adopts checkpoint+tail, and crosses its fit gate."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [None, "owned", "independent"])
async def test_native_checkpoint_hold_exits_by_install_or_independent_stop(
    isolated_db, monkeypatch, tmp_path, stop,
):
    from core import checkpoint_runtime as runtime, turn_iteration
    from core.checkpoint_resume import installed_replay
    from core.context_builder import ContextBuilder
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.native_context_prefix import build_real_send_prefix
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import TurnIdentity
    from infra.checkpoint_queries import current_installed
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from llm.router import LLMResponse
    from test_checkpoint_hold_install_resume import (
        _AFTER_STOP, _reach_wait, _seed, _start_checkpoint,
    )

    target = replace(resolve_execution_target("openai/test-checkpoint"), context_window=32768)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    db = get_db()
    _seed(db, target.catalog_id)
    db.execute("UPDATE checkpoints SET source_harness='native',writer_harness='native'")
    db.commit()
    selection = installed_replay(current_installed("held-thread", db=db))
    db.close()
    agent = SimpleNamespace(context=ContextBuilder(tmp_path), temperature=0.7,
        max_iterations=1, tools=SimpleNamespace(get_definitions=lambda: []),
        _strip_think=lambda content: content)
    messages, _ = await build_real_send_prefix(
        agent=agent, thread_id="held-thread", turn_model=target.catalog_id,
        fresh_context=False, project_id=None, model_context_window=target.context_window,
        replay_selection=selection, tool_definitions=[], selected_model=target.catalog_id,
    )
    fit = attach_native_provider_fit_ledger(
        model=target.catalog_id, target_catalog_id=target.catalog_id,
        proven_target_window_tokens=target.context_window, messages=messages,
        tools=[], input_identity="turn-current", current_input=messages[-1],
    ).fit_ledger
    assert not fit.fits
    state = turn_iteration._IterationState(
        messages=messages, context_usage={"checkpoint_generation_id": "floor"},
        tool_definitions=[], context_identity=None, fit_ledger=fit,
        fit_current_input=messages[-1], checkpoint_source_messages=list(messages),
        turn_model=target.catalog_id, provider_delivery_settled=True,
    )
    identity = TurnIdentity("turn-current", 0)
    cancel = TurnCancelEvent()
    cancel.bind_turn_identity(identity)
    release, waiting, await_original = await _start_checkpoint(
        monkeypatch, target, tmp_path, identity, owned=stop != "independent",
    )
    calls, events = [], []

    async def completion(messages, tools, **kwargs):
        # Only inference is synthetic: the real router fit check precedes us.
        calls.append((messages, tools, kwargs))
        return LLMResponse("done", usage={"prompt_tokens": 100, "completion_tokens": 1})

    async def on_step(kind, payload):
        events.append((kind, payload))

    monkeypatch.setattr("llm.providers.openai.openai_completion", completion)
    task = asyncio.create_task(turn_iteration._run_loop(
        agent, state, "held-thread", target.catalog_id, {}, cancel, on_step,
        None, None, None, identity, None,
    ))
    try:
        await _reach_wait(task, waiting)
        assert not calls and not task.done()
        if stop:
            cancel.checkpoint_wait_scope.request_stop()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await runtime._read("new-checkpoint")).status == _AFTER_STOP[stop]
        release.set()
        record = await asyncio.wait_for(await_original("new-checkpoint"), 5)
        assert record.status == ("cancelled" if stop == "owned" else "installed")
        if stop:
            assert not calls
            assert events[-1][0] == "turn_cancelled"
        else:
            assert await task == (1, [])
            assert len(calls) == 1
            payload = json.dumps(calls[0][0])
            for retained in ("NEW_CHECKPOINT_BODY", "CURRENT_INPUT_RETAINED", "EXACT_DURABLE_TOOL_RESULT"):
                assert retained in payload
            assert "ARCHIVE_NOT_REPLAYED" not in payload and "FOLDED_HISTORY" not in payload
            assert state.fit_ledger.fits
            assert state.fit_ledger.rendered_input_tokens <= target.context_window
            assert state.context_usage["checkpoint_generation_id"] == record.id
            assert events[-1][0] == "turn_complete"
        assert cancel.turn_identity == identity
        assert cancel.checkpoint_wait_scope.checkpoint_id is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown_checkpoint_tasks()
