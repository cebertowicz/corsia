from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cache_warm_fixtures import (
    cleanup_cache_warm_isolation,
    install_cache_warm_isolation,
    install_replay_candidates,
    install_resolved_omlx_session,
)
from native_checkpoint_fixtures import (
    exact_selection,
    install_prefix_source_isolation,
    native_target,
    seed_native_thread,
)


@pytest.fixture(autouse=True)
def _isolate_warm_owner(monkeypatch):
    state = install_cache_warm_isolation(monkeypatch)
    install_prefix_source_isolation(monkeypatch)
    yield
    cleanup_cache_warm_isolation(state)


def _tool_registry():
    from tools.base import Tool
    from tools.registry import ToolRegistry

    class NamedTool(Tool):
        def __init__(self, name: str):
            self._name = name

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return self._name

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **_kwargs) -> str:
            return "{}"

    registry = ToolRegistry()
    registry.register(NamedTool("read_file"))
    registry.register(NamedTool("update_plan"))
    return registry


@pytest.mark.asyncio
async def test_native_warm_send_and_checkpoint_freeze_same_external_tools(
    monkeypatch, main_db,
) -> None:
    from api import thread_cache_warm as thread_runtime
    from core import fork_native, native_context_prefix, turn_context
    from infra import state
    selection = exact_selection()
    boundary = selection.sealed_boundary
    seed_native_thread(main_db, selection)
    install_replay_candidates(monkeypatch, exact=selection)
    registry = _tool_registry()
    agent = SimpleNamespace(
        tools=registry,
        _connect_mcp=AsyncMock(),
        max_tokens=8_192,
    )
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    captured_send: dict = {}
    async def prepare_turn_context(*_args, tool_definitions, **_kwargs):
        captured_send["tools"] = tool_definitions
        return ([{"role": "user", "content": "tail"}], {})

    monkeypatch.setattr(turn_context, "prepare_turn_context", prepare_turn_context)
    prepared = await turn_context.prepare_native_context_payload(
        thread_id=boundary.thread_id,
        user_content="tail",
        target=native_target(),
        project_id=None,
        media=None,
        replay_selection=selection,
    )
    install_resolved_omlx_session(
        monkeypatch,
        thread_runtime,
        "omlx@srv-1/model-1",
    )
    captured_warm: dict = {}
    async def build_prefix(**kwargs):
        captured_warm["tools"] = kwargs["tool_definitions"]
        return (
            [{"role": "system", "content": "prefix"}],
            {"history_messages": 0, "history_tokens": 0, "was_trimmed": False},
        )

    monkeypatch.setattr(native_context_prefix, "build_real_send_prefix", build_prefix)
    provider = MagicMock()
    provider.probe_cache = AsyncMock(return_value={"ok": False})
    provider.warm_cache = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)
    response = await thread_runtime.warm_session_cache(boundary.thread_id, token="t")
    assert response["ok"] is True
    pending = state._active_warm_tasks.get(boundary.thread_id)
    if pending is not None:
        await pending
    fork = fork_native.capture_live_warm_source(
        boundary.thread_id,
        boundary,
        messages=list(prepared.messages),
        tools=list(prepared.tool_definitions),
        reasoning_effort=None,
        preserve_thinking=None,
        provider_input_tokens=1_000,
        rendered_input_tokens=1_000,
    )
    expected = ({"type": "function", "function": {
        "name": "read_file",
        "description": "read_file",
        "parameters": {"type": "object", "properties": {}},
    }},)
    assert prepared.tool_definitions == expected
    assert captured_send["tools"] == expected
    assert captured_warm["tools"] == expected
    assert fork.tool_definitions == expected
    tool_names = {tool["function"]["name"] for tool in prepared.tool_definitions}
    assert "read_file" in tool_names
    assert "update_plan" not in tool_names


def _omlx_target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id="oMLX/model-1",
        harness="native",
        provider="omlx",
        backend="network",
        transport_model="omlx@srv-1/model-1",
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="model-1",
        supports_reasoning=False,
    )


def _prepared_context():
    from core.turn_context import NativePreparedContext

    return NativePreparedContext(
        messages=(
            {"role": "system", "content": "stable prefix"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "near-tail edit"},
        ),
        context_usage={},
        tool_definitions=(
            {"type": "function", "function": {"name": "read_file"}},
        ),
        context_identity=None,
    )


def _install_preflight_shell(monkeypatch, *, ws, on_delivery):
    from core.thread_warm import join_requested_warm_for_delivery

    prepared = _prepared_context()

    class FakePreflight:
        async def execute(self):
            await join_requested_warm_for_delivery("thread-native", "omlx@srv-1/model-1")
            return await on_delivery({"provider": "native", "ws": ws})

    return FakePreflight(), prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("ws", [object(), None], ids=("browser", "event"))
async def test_send_joins_existing_registered_warm_before_delivery(monkeypatch, ws) -> None:
    from core.thread_warm import start_thread_warm, warm_prefix_key

    release = asyncio.Event()
    delivered = asyncio.Event()
    prepared = _prepared_context()
    key = warm_prefix_key(
        "thread-native",
        "model-1",
        list(prepared.messages[:-1]),
        list(prepared.tool_definitions),
    )

    async def matching_warm():
        await release.wait()
        return {"ok": True}

    warm_task, _, _ = await start_thread_warm(
        "thread-native", key, matching_warm, model="omlx@srv-1/model-1", admission_check=lambda: None,
    )

    async def delivery(_kwargs):
        delivered.set()
        return "delivered"

    preflight, _ = _install_preflight_shell(
        monkeypatch, ws=ws, on_delivery=delivery,
    )
    send_task = asyncio.create_task(preflight.execute())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not delivered.is_set()
        release.set()
        assert await send_task == "delivered"
        assert warm_task is not None and warm_task.done()
    finally:
        release.set()
        await asyncio.gather(send_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_continues_when_joined_warm_is_replaced(monkeypatch) -> None:
    from core.thread_warm import start_thread_warm

    old_started = asyncio.Event()
    old_cancelled = asyncio.Event()
    replacement_started = asyncio.Event()

    async def old_warm():
        old_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_cancelled.set()
            raise

    old_task, _, _ = await start_thread_warm(
        "thread-native", "prefix-old", old_warm, model="omlx@srv-1/model-1", admission_check=lambda: None,
    )
    assert old_task is not None
    await old_started.wait()

    async def delivery(_kwargs):
        return "delivered"

    preflight, _ = _install_preflight_shell(
        monkeypatch, ws=object(), on_delivery=delivery,
    )
    send_task = asyncio.create_task(preflight.execute())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    async def replacement_warm():
        replacement_started.set()
        return {"ok": True}

    replacement = await start_thread_warm(
        "thread-native",
        "prefix-new",
        replacement_warm,
        model="omlx@srv-1/model-1", admission_check=lambda: "runnable_delivery",
    )
    send_result = await asyncio.gather(send_task, return_exceptions=True)

    assert old_cancelled.is_set()
    assert old_task.cancelled()
    assert replacement == (None, False, "runnable_delivery")
    assert not replacement_started.is_set()
    assert send_result == ["delivered"]


@pytest.mark.asyncio
async def test_send_without_registered_warm_does_not_probe_or_prefill(
    monkeypatch,
) -> None:
    provider_calls: list[str] = []

    def unexpected_provider(name: str):
        provider_calls.append(name)
        return MagicMock()

    monkeypatch.setattr(
        "llm.providers.for_provider",
        unexpected_provider,
    )

    async def delivery(_kwargs):
        return "delivered"

    preflight, _ = _install_preflight_shell(
        monkeypatch, ws=object(), on_delivery=delivery,
    )

    assert await preflight.execute() == "delivered"
    assert provider_calls == []


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_or_cancel_registered_warm(monkeypatch) -> None:
    from core.thread_warm import start_thread_warm, warm_prefix_key

    release = asyncio.Event()
    prepared = _prepared_context()
    key = warm_prefix_key(
        "thread-native",
        "model-1",
        list(prepared.messages[:-1]),
        list(prepared.tool_definitions),
    )

    async def matching_warm():
        await release.wait()
        return {"ok": True}

    warm_task, _, _ = await start_thread_warm(
        "thread-native", key, matching_warm, model="omlx@srv-1/model-1", admission_check=lambda: None,
    )

    async def delivery(_kwargs):
        raise AssertionError("cancelled send reached provider delivery")

    preflight, _ = _install_preflight_shell(
        monkeypatch, ws=object(), on_delivery=delivery,
    )
    send_task = asyncio.create_task(preflight.execute())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    send_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(send_task, timeout=0.1)
    assert warm_task is not None
    assert not warm_task.done()

    release.set()
    assert await warm_task == {"ok": True}
