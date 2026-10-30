from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import (
    native_turn_runtime,
    turn_iteration,
    turn_iteration_context,
    turn_provider_call,
    turns,
    turns_steps,
    turns_util,
)
from core.turn_core import TurnIdentity, TurnTerminalResult
from infra.thread_driver import TerminalSettlement


def _empty_replay(thread_id: str):
    from core.context_replay import ReplaySelection
    from history.types import PortableHistoryProjection

    return ReplaySelection(
        mode="exact",
        history_epoch=0,
        sealed_boundary=None,
        tail=PortableHistoryProjection(
            format_version=1,
            thread_id=thread_id,
            history_epoch=0,
            start_exclusive=None,
            sealed_boundary=None,
            turns=(),
            full_raw_replay_available=True,
        ),
    )


@pytest.mark.asyncio
async def test_native_preflight_keeps_usage_live():
    prepared = SimpleNamespace(
        messages=[{"role": "user", "content": "hello"}],
        context_usage={"snapshot_id": "ctx-live-only"},
        tool_definitions=[],
        context_identity=None,
        fit_ledger=None,
    )
    state = await turn_iteration._prepare_iteration_state(
        _FakeAgent(),
        "native-live-only",
        "hello",
        "native/test",
        False,
        None,
        "web",
        "native-live-only",
        None,
        32_000,
        prepared,
    )

    assert state.context_usage == {"snapshot_id": "ctx-live-only"}


def test_openai_compatible_cached_tokens_are_not_added_twice():
    assert turn_iteration_context.effective_input_tokens({
        "prompt_tokens": 1_000,
        "cache_read_tokens": 800,
    }) == 1_000


def test_anthropic_effective_input_sums_disjoint_cache_fields_once():
    from llm.providers.anthropic_streaming import _merge_stream_usage
    from llm.providers.anthropic_translation import _parse_usage

    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=5,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=700,
    )

    assert _parse_usage(usage)["effective_input_tokens"] == 1_000
    assert _merge_stream_usage({}, usage)["effective_input_tokens"] == 1_000


class _FakeTools:
    def get_definitions(self):
        return []


class _FakeAgent:
    model = "openai/test-model"
    max_iterations = 2
    max_tokens = 100
    tools = _FakeTools()

    async def _connect_mcp(self):
        return None

    def _strip_think(self, content: str | None):
        return content


@pytest.mark.parametrize(
    ("invalid_value", "error_type"),
    [
        (float("nan"), ValueError),
        (object(), TypeError),
    ],
)
def test_native_tool_schema_ledger_rejects_non_json_values(
    invalid_value,
    error_type,
):
    from core.native_context_prefix import _canonical_tool_definitions

    with pytest.raises(error_type):
        _canonical_tool_definitions(({"invalid": invalid_value},))


@pytest.mark.asyncio
async def test_native_preflight_freezes_one_exact_nonzero_tool_ledger(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """The selected schemas must be the same bytes counted, proved, and sent."""
    from core.turn_context import prepare_native_context_payload
    from core.token_estimation import _estimate_tokens
    from infra.database import get_db
    from infra.time import utc_now_iso

    class ChangingTools:
        def __init__(self):
            self.calls = 0

        def get_definitions(self):
            self.calls += 1
            return [{
                "type": "function",
                "function": {
                    # Native and external harnesses now share the catalog gate;
                    # vary a cataloged definition to keep proving one freeze.
                    "name": "read_file",
                    "description": f"Frozen schema {self.calls}",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }]

    class Context:
        workspace = tmp_path

        @staticmethod
        def build_system_prompt(project_id=None):
            return "Native identity and bootstrap"

    tools = ChangingTools()
    agent = SimpleNamespace(
        context=Context(),
        tools=tools,
        memory_window=100,
        max_tokens=256,
        _connect_mcp=AsyncMock(return_value=None),
    )

    async def get_agent():
        return agent

    monkeypatch.setattr("core.agent.get_agent", get_agent)
    now = utc_now_iso()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("native-tool-ledger", "steve", "native/test", now, now),
        )
        db.commit()
    finally:
        db.close()

    prepared = await prepare_native_context_payload(
        thread_id="native-tool-ledger",
        user_content="hello",
        target=SimpleNamespace(
            catalog_id="native/test",
            transport_model="native/test",
            context_window=32_000,
        ),
        project_id=None,
        media=None,
        replay_selection=_empty_replay("native-tool-ledger"),
        runtime_context_epoch=7,
    )

    assert tools.calls == 1
    assert prepared.context_identity.harness == "native"
    assert prepared.context_identity.catalog_model_id == "native/test"
    assert prepared.context_identity.runtime_context_epoch == 7
    assert prepared.context_identity.producer_handle_fingerprint is None
    assert prepared.tool_definitions[0]["function"]["name"] == "read_file"
    assert prepared.tool_definitions[0]["function"]["description"] == "Frozen schema 1"
    snapshot = prepared.context_usage["context_snapshot"]
    tools_rows = [
        row for row in snapshot["source_inventory"]
        if row["origin_section"] == "tools" and row["inclusion"] == "included"
    ]
    assert len(tools_rows) == 1
    canonical = json.dumps(
        prepared.tool_definitions,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    assert tools_rows[0]["prompt_tokens"] == _estimate_tokens(canonical) + 4
    assert tools_rows[0]["prompt_tokens"] == prepared.context_usage["tool_def_tokens"]
    assert (
        snapshot["frozen_prompt_material"]["provider_payload"]
        ["tool_definitions"]
        == list(prepared.tool_definitions)
    )
    assert len(prepared.context_usage["source_artifacts"]) == 2
    assert all(
        artifact["payload"]["tools"] == list(prepared.tool_definitions)
        for artifact in prepared.context_usage["source_artifacts"]
    )
    assert not any(
        row["source_id"] == "native-system-prompt"
        for row in snapshot["source_inventory"]
    )


def _preflight_usage() -> dict:
    return {
        "model_context_window": 1_000,
        "system_tokens": 40,
        "tool_def_tokens": 0,
        "history_tokens": 20,
        "user_tokens": 10,
        "max_tokens": 100,
        "total_used": 170,
        "utilization": 0.17,
        "was_trimmed": False,
        "history_messages": 1,
        "snapshot_id": "ctx-native-final-call",
        "context_snapshot": {
            "snapshot_id": "ctx-native-final-call",
            "harness_model_facts": {"model_context_window": 1_000},
            "source_inventory": [{
                "source_id": "system",
                "origin_section": "system",
                "inclusion": "included",
                "prompt_tokens": 40,
            }],
            "frozen_prompt_material": {},
        },
        "source_artifacts": [],
        "continuity_mode": "full",
    }


def _prepared_native_context():
    from infra.context_receipts import ContextReceiptIdentity

    return SimpleNamespace(
        messages=({"role": "user", "content": "go"},),
        context_usage=_preflight_usage(),
        tool_definitions=(),
        context_identity=ContextReceiptIdentity(
            "native",
            "openai/test-model",
            0,
        ),
        fit_ledger=None,
    )


@pytest.mark.asyncio
async def test_native_turn_emits_final_call_occupancy_for_terminal_persistence(monkeypatch):
    from core import step_handler
    from core.step_handler import StepHandler
    from core.turn_core import TurnIdentity

    responses = iter([
        SimpleNamespace(
            content="checking",
            finish_reason="tool_calls",
            has_tool_calls=True,
            usage={
                "prompt_tokens": 100,
                "cache_read_tokens": 60,
                "completion_tokens": 5,
            },
            reasoning_content=None,
            ttft_ms=None,
        ),
        SimpleNamespace(
            content="done",
            finish_reason="stop",
            has_tool_calls=False,
            usage={
                "prompt_tokens": 180,
                "cache_read_tokens": 140,
                "completion_tokens": 8,
            },
            reasoning_content=None,
            ttft_ms=None,
        ),
    ])

    async def fake_call_llm(*_args, **_kwargs):
        return next(responses), False

    async def fake_run_in_db(*_args, **_kwargs):
        return None

    emitted: list[tuple[str, dict]] = []
    attached: list[dict] = []

    async def fake_handler_run_in_db(fn, *_args, **kwargs):
        if fn is step_handler._attach_delivered_context_to_latest_assistant:
            attached.append(kwargs["context_usage"])
            return "assistant-row", kwargs["context_usage"]
        return None

    broadcasts: list[dict] = []

    async def broadcast(payload: dict):
        broadcasts.append(payload)

    handler = StepHandler(
        "session-native-final-call",
        {"model": "openai/test-model"},
        broadcast,
        "turn-native-final-call",
    )
    handler.bind_turn_identity(TurnIdentity("turn-native-final-call", 1))
    handler.step_ids.append("assistant-row")

    async def on_step(event: str, payload: dict):
        emitted.append((event, payload))
        if event == "turn_complete":
            await handler._handle_turn_complete(payload)

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(turn_provider_call, "call_llm", fake_call_llm)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(step_handler, "run_in_db", fake_handler_run_in_db)
    monkeypatch.setattr(
        turns_steps,
        "execute_tool_calls",
        AsyncMock(return_value=[{"role": "tool", "content": "result"}]),
    )
    monkeypatch.setattr(turn_iteration, "run_in_db", fake_run_in_db)

    await turns.run_turn_with_steps(
        _FakeAgent(),
        "go",
        "session-native-final-call",
        "web",
        "session-native-final-call",
        on_step,
        asyncio.Event(),
        model="openai/test-model",
        turn_provider={"uses_streaming": False},
        prepared_context=_prepared_native_context(),
    )

    terminal = next(payload for event, payload in emitted if event == "turn_complete")
    usage = terminal["context_usage"]
    assert usage["total_used"] == 180
    assert usage["provider_reported_total_used"] == 180
    assert usage["provider_reported_input_tokens"] == 180
    assert usage["provider_reported_cache_read_input_tokens"] == 140
    assert usage["utilization"] == 0.18
    assert usage["provider_reported_utilization"] == 0.18
    assert usage["token_usage_available"] is True
    assert usage["token_source"] == "measured"
    assert attached == [usage]
    assert broadcasts[0]["context_message_id"] == "assistant-row"


@pytest.mark.asyncio
async def test_native_terminal_without_measurement_is_unavailable_not_preflight_estimate(monkeypatch):
    async def fake_call_llm(*_args, **_kwargs):
        return (
            SimpleNamespace(
                content="done",
                finish_reason="stop",
                has_tool_calls=False,
                usage={},
                reasoning_content=None,
                ttft_ms=None,
            ),
            False,
        )

    async def fake_run_in_db(*_args, **_kwargs):
        return None

    emitted: list[tuple[str, dict]] = []

    async def on_step(event: str, payload: dict):
        emitted.append((event, payload))

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(turn_provider_call, "call_llm", fake_call_llm)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(turn_iteration, "run_in_db", fake_run_in_db)

    await turns.run_turn_with_steps(
        _FakeAgent(),
        "go",
        "session-native-unmeasured",
        "web",
        "session-native-unmeasured",
        on_step,
        asyncio.Event(),
        model="openai/test-model",
        turn_provider={"uses_streaming": False},
    )

    terminal = next(payload for event, payload in emitted if event == "turn_complete")
    usage = terminal["context_usage"]
    assert usage["total_used"] is None
    assert usage["utilization"] is None
    assert usage["token_usage_available"] is False
    assert usage.get("token_source") is None


@pytest.mark.asyncio
async def test_native_cancellation_persists_last_measured_call_occupancy(monkeypatch):
    from core import step_handler
    from core.step_handler import StepHandler

    cancel_event = asyncio.Event()
    response = SimpleNamespace(
        content="checking",
        finish_reason="tool_calls",
        has_tool_calls=True,
        usage={
            "prompt_tokens": 120,
            "cache_read_tokens": 80,
            "completion_tokens": 5,
        },
        reasoning_content=None,
        ttft_ms=None,
    )

    async def fake_execute_tool_calls(*_args, **_kwargs):
        cancel_event.set()
        return [{"role": "tool", "content": "result"}]

    attached: list[dict] = []

    async def fake_handler_run_in_db(fn, *_args, **kwargs):
        if fn is step_handler._attach_delivered_context_to_latest_assistant:
            attached.append(kwargs["context_usage"])
            return "assistant-row", kwargs["context_usage"]
        return None

    async def fake_terminal(*_args, **_kwargs):
        return TerminalSettlement(TurnTerminalResult.APPLIED, None)

    broadcasts: list[dict] = []

    async def broadcast(payload: dict):
        broadcasts.append(payload)

    identity = TurnIdentity("turn-native-cancelled-occupancy", 1)
    handler = StepHandler(
        "session-native-cancelled-occupancy",
        {"model": "openai/test-model"},
        broadcast,
        identity.turn_id,
    )
    handler.bind_turn_identity(identity)
    handler.step_ids.append("assistant-row")

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(
        turn_provider_call,
        "call_llm",
        AsyncMock(return_value=(response, False)),
    )
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(turns_steps, "execute_tool_calls", fake_execute_tool_calls)
    monkeypatch.setattr(turn_iteration, "run_in_db", AsyncMock(return_value=None))
    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", fake_terminal)
    monkeypatch.setattr(step_handler, "run_in_db", fake_handler_run_in_db)

    was_cancelled = await native_turn_runtime.run_tracked_turn(native_turn_runtime.NativeTurn(
        agent=_FakeAgent(),
        thread_id="session-native-cancelled-occupancy",
        content="go",
        model="openai/test-model",
        turn_provider={"uses_streaming": False},
        handler=handler,
        cancel_event=cancel_event,
        identity=identity,
        prepared_context=_prepared_native_context(),
    ))

    assert was_cancelled is True
    assert len(attached) == 1
    usage = attached[0]
    assert usage["total_used"] == 120
    assert usage["token_source"] == "measured"
    terminal = next(event for event in broadcasts if event["type"] == "turn_cancelled")
    assert terminal["context_message_id"] == "assistant-row"
    assert terminal["context_usage"]["total_used"] == 120


@pytest.mark.asyncio
async def test_native_provider_error_persists_last_successful_call_occupancy(monkeypatch):
    from core import step_handler
    from core.step_handler import StepHandler

    responses = iter([
        SimpleNamespace(
            content="checking",
            finish_reason="tool_calls",
            has_tool_calls=True,
            usage={
                "prompt_tokens": 140,
                "cache_read_tokens": 90,
                "completion_tokens": 5,
            },
            reasoning_content=None,
            ttft_ms=None,
        ),
        SimpleNamespace(
            content="provider failed",
            finish_reason="error",
            error_kind=None,
            resets_at=None,
            own_server=False,
            has_tool_calls=False,
            usage={
                "prompt_tokens": 999,
                "cache_read_tokens": 900,
                "completion_tokens": 1,
            },
            reasoning_content=None,
            ttft_ms=None,
        ),
    ])

    async def fake_call_llm(*_args, **_kwargs):
        return next(responses), False

    attached: list[dict] = []

    async def fake_handler_run_in_db(fn, *_args, **kwargs):
        if fn is step_handler._attach_delivered_context_to_latest_assistant:
            attached.append(kwargs["context_usage"])
            return "assistant-row", kwargs["context_usage"]
        if fn is step_handler.persist_llm_error:
            return {"turn_id": identity.turn_id, "reference": "ABC123", "next_step": "failed"}
        return None

    async def fake_terminal(*_args, **_kwargs):
        return TerminalSettlement(TurnTerminalResult.APPLIED, None)

    broadcasts: list[dict] = []

    async def broadcast(payload: dict):
        broadcasts.append(payload)

    identity = TurnIdentity("turn-native-error-occupancy", 1)
    handler = StepHandler(
        "session-native-error-occupancy",
        {"model": "openai/test-model"},
        broadcast,
        identity.turn_id,
    )
    handler.bind_turn_identity(identity)
    handler.step_ids.append("assistant-row")

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(turn_provider_call, "call_llm", fake_call_llm)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(
        turns_steps,
        "execute_tool_calls",
        AsyncMock(return_value=[{"role": "tool", "content": "result"}]),
    )
    monkeypatch.setattr(turn_iteration, "run_in_db", AsyncMock(return_value=None))
    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", fake_terminal)
    monkeypatch.setattr(step_handler, "run_in_db", fake_handler_run_in_db)

    with pytest.raises(turn_iteration.TurnProviderError, match="provider failed"):
        await native_turn_runtime.run_tracked_turn(native_turn_runtime.NativeTurn(
            agent=_FakeAgent(),
            thread_id="session-native-error-occupancy",
            content="go",
            model="openai/test-model",
            turn_provider={"uses_streaming": False},
            handler=handler,
            cancel_event=asyncio.Event(),
            identity=identity,
            prepared_context=_prepared_native_context(),
        ))

    assert len(attached) == 1
    usage = attached[0]
    assert usage["total_used"] == 140
    assert usage["token_source"] == "measured"
    terminal = next(event for event in broadcasts if event["type"] == "error")
    assert terminal["context_message_id"] == "assistant-row"
    assert terminal["context_usage"]["total_used"] == 140


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "effect_kind"),
    [
        (RuntimeError("tool execution failed"), "error"),
        (asyncio.CancelledError(), "turn_cancelled"),
    ],
)
async def test_native_unexpected_terminal_retains_last_measured_occupancy(
    monkeypatch,
    failure,
    effect_kind,
):
    response = SimpleNamespace(
        content="checking",
        finish_reason="tool_calls",
        has_tool_calls=True,
        usage={
            "prompt_tokens": 160,
            "cache_read_tokens": 100,
            "completion_tokens": 5,
        },
        reasoning_content=None,
        ttft_ms=None,
    )

    async def fail_tool_execution(*_args, **_kwargs):
        raise failure

    async def fake_terminal(*_args, **_kwargs):
        return TerminalSettlement(TurnTerminalResult.APPLIED, None)

    handler = SimpleNamespace(handle=AsyncMock())
    identity = TurnIdentity(
        f"turn-native-unexpected-{effect_kind}",
        1,
    )

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(
        turn_provider_call,
        "call_llm",
        AsyncMock(return_value=(response, False)),
    )
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(turns_steps, "execute_tool_calls", fail_tool_execution)
    monkeypatch.setattr(turn_iteration, "run_in_db", AsyncMock(return_value=None))
    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", fake_terminal)

    with pytest.raises(type(failure)):
        await native_turn_runtime.run_tracked_turn(native_turn_runtime.NativeTurn(
            agent=_FakeAgent(),
            thread_id=f"session-native-unexpected-{effect_kind}",
            content="go",
            model="openai/test-model",
            turn_provider={"uses_streaming": False},
            handler=handler,
            cancel_event=asyncio.Event(),
            identity=identity,
        ))

    assert handler.handle.await_count == 2
    boundary, terminal = handler.handle.await_args_list
    assert boundary.args[0] == "context_usage"
    assert boundary.args[1]["context_usage"]["total_used"] == 160
    kind, payload = terminal.args
    assert kind == effect_kind
    assert payload["context_usage"]["total_used"] == 160
    assert payload["context_usage"]["token_source"] == "measured"


@pytest.mark.asyncio
async def test_native_telemetry_failure_retains_returned_call_occupancy(monkeypatch):
    response = SimpleNamespace(
        content="done",
        finish_reason="stop",
        has_tool_calls=False,
        usage={"prompt_tokens": 175, "completion_tokens": 5},
        reasoning_content=None,
        ttft_ms=None,
    )

    async def fail_usage_log(fn, *_args, **_kwargs):
        if fn is not turns_util._log_llm_usage:
            return None  # the call-start binding capture
        raise RuntimeError("usage telemetry unavailable")

    async def fake_terminal(*_args, **_kwargs):
        return TerminalSettlement(TurnTerminalResult.APPLIED, None)

    handler = SimpleNamespace(handle=AsyncMock())
    identity = TurnIdentity("turn-native-telemetry-failure", 1)

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(
        turn_provider_call,
        "call_llm",
        AsyncMock(return_value=(response, False)),
    )
    monkeypatch.setattr(turn_iteration, "run_in_db", fail_usage_log)
    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", fake_terminal)

    with pytest.raises(RuntimeError, match="usage telemetry unavailable"):
        await native_turn_runtime.run_tracked_turn(native_turn_runtime.NativeTurn(
            agent=_FakeAgent(),
            thread_id="session-native-telemetry-failure",
            content="go",
            model="openai/test-model",
            turn_provider={"uses_streaming": False},
            handler=handler,
            cancel_event=asyncio.Event(),
            identity=identity,
        ))

    assert handler.handle.await_count == 2
    boundary, terminal = handler.handle.await_args_list
    assert boundary.args[0] == "context_usage"
    assert boundary.args[1]["context_usage"]["total_used"] == 175
    kind, payload = terminal.args
    assert kind == "error"
    assert payload["context_usage"]["total_used"] == 175


@pytest.mark.asyncio
async def test_native_cancel_during_final_text_selects_cancel_terminal(monkeypatch):
    response = SimpleNamespace(
        content="done",
        finish_reason="stop",
        has_tool_calls=False,
        usage={"prompt_tokens": 185, "completion_tokens": 5},
        reasoning_content=None,
        ttft_ms=None,
    )
    cancel_event = asyncio.Event()
    handled: list[tuple[str, dict]] = []

    async def handle(kind: str, payload: dict):
        handled.append((kind, payload))
        if kind == "text":
            cancel_event.set()

    terminal_outcomes: list[str] = []

    async def fake_terminal(*_args, **_kwargs):
        terminal_outcomes.append(_args[1])
        return TerminalSettlement(TurnTerminalResult.APPLIED, None)

    identity = TurnIdentity("turn-native-final-text-cancel", 1)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], _preflight_usage())),
    )
    monkeypatch.setattr(
        turn_provider_call,
        "call_llm",
        AsyncMock(return_value=(response, False)),
    )
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(turn_iteration, "run_in_db", AsyncMock(return_value=None))
    monkeypatch.setattr(native_turn_runtime, "commit_turn_terminal", fake_terminal)

    was_cancelled = await native_turn_runtime.run_tracked_turn(native_turn_runtime.NativeTurn(
        agent=_FakeAgent(),
        thread_id="session-native-final-text-cancel",
        content="go",
        model="openai/test-model",
        turn_provider={"uses_streaming": False},
        handler=SimpleNamespace(handle=handle),
        cancel_event=cancel_event,
        identity=identity,
    ))

    assert was_cancelled is True
    assert terminal_outcomes == ["cancelled"]
    assert [kind for kind, _payload in handled] == [
        "context_usage", "text", "turn_cancelled",
    ]
    assert handled[-1][1]["context_usage"]["total_used"] == 185
