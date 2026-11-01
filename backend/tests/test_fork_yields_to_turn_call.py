"""On a local oMLX seat a Native fork starts no call while its thread's Turn call runs.

oMLX's prefill memory guard charges both sequences, so on a 32 GB Mac a warm
checkpoint call beside the Turn's ~31k call got the Turn refused (dev.917).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.fork_native_tools import yield_to_turn_call
from infra import state
from llm.router import LLMResponse
from tests.test_native_cold_round_trip import THREAD, _admission

LOCAL_SEAT = {"provider": "omlx", "backend": "network", "transport_model": "omlx@ns-test/Qwen-test"}


def _request(target_fields):
    target = SimpleNamespace(harness="native", **target_fields)
    return SimpleNamespace(target=target, thread_id=THREAD)


@pytest_asyncio.fixture
async def turn_call(monkeypatch):
    release = asyncio.Event()

    async def provider_call():
        await release.wait()
        return "turn answer"

    task = asyncio.get_running_loop().create_task(provider_call())
    monkeypatch.setitem(state._active_llm_tasks, THREAD, task)
    yield task, release
    task.cancel()


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_local_fork_waits_for_the_turn_call_and_leaves_it_alone(turn_call):
    task, release = turn_call
    gate = asyncio.create_task(yield_to_turn_call(_request(LOCAL_SEAT), asyncio.Event()))
    await _settle()
    assert not gate.done()

    release.set()
    await gate
    assert await task == "turn answer"


@pytest.mark.asyncio
async def test_cloud_fork_does_not_wait(turn_call):
    cloud = {"provider": "anthropic", "backend": "network", "transport_model": "claude-test"}
    await asyncio.wait_for(yield_to_turn_call(_request(cloud), asyncio.Event()), 1)


@pytest.mark.asyncio
async def test_fork_stop_while_waiting_ends_the_fork_not_the_turn(turn_call):
    task, release = turn_call
    stop = asyncio.Event()
    gate = asyncio.create_task(yield_to_turn_call(_request(LOCAL_SEAT), stop))
    await _settle()

    stop.set()
    with pytest.raises(asyncio.CancelledError):
        await gate
    assert not task.done()
    release.set()
    assert await task == "turn answer"


@pytest.mark.asyncio
async def test_warm_fold_sends_only_after_the_turn_call_ends(monkeypatch, turn_call):
    from core import fork_cold_count, fork_native as native

    _task, release = turn_call
    monkeypatch.setattr(fork_cold_count, "_server_tokens", lambda text, _writer: len(text) // 4)
    request = _admission(monkeypatch).provider_request
    request = replace(request, target=replace(request.target, **LOCAL_SEAT))
    sent = asyncio.Event()

    async def get_agent():
        return SimpleNamespace(max_iterations=1, tools=SimpleNamespace(get_definitions=lambda: []))

    async def completion(**_kwargs):
        sent.set()
        return LLMResponse(content="synthetic checkpoint", usage={})

    monkeypatch.setattr(native, "_get_agent", get_agent)
    monkeypatch.setattr(native, "_provider_params", lambda _: {"model": "Qwen-test"})
    monkeypatch.setattr(native, "_chat_completion", completion)

    fold = asyncio.create_task(native.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if fold.done():
            break
    assert not sent.is_set()

    release.set()
    result = await asyncio.wait_for(fold, 5)
    assert result.raw_body == "synthetic checkpoint"
