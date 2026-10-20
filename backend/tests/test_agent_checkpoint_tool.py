"""Agent-requested checkpoint contracts, kept independent of provider execution."""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.checkpoint_types import CheckpointRequest, ProviderCheckpointRequest
from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS
from llm.resolution import ExecutionTarget


THREAD = "agent-checkpoint-thread"


def _request(trigger: str = "agent") -> CheckpointRequest:
    return CheckpointRequest(
        id="agent-cp",
        thread_id=THREAD,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=9,
        through_message_id="m-9",
        trigger=trigger,
        mode="warm",
        source_harness="codex",
        source_model="codex/test",
        writer_harness="codex",
        writer_model="codex/test",
        originating_turn_id=None,
        first_included_seq=None,
        started_at="t0",
    )


def _provider() -> ProviderCheckpointRequest:
    target = ExecutionTarget(
        catalog_id="codex/test", harness="codex", provider="openai", backend="api",
        transport_model="test", expected_reported_model=None, context_window=200_000,
        available=True, availability_reason=None, display_name="test",
        supports_reasoning=True, preserve_thinking_default=True,
    )
    return ProviderCheckpointRequest(
        fork_id="agent-cp", thread_id=THREAD, target=target,
        instruction="checkpoint", reasoning_effort=None, working_dir="/tmp",
    )


def _claude_context() -> SimpleNamespace:
    target = ExecutionTarget(
        catalog_id="claude-code/sonnet", harness="claude-code",
        provider="anthropic", backend="cc", transport_model="sonnet",
        expected_reported_model=None, context_window=200_000, available=True,
        availability_reason=None, display_name="sonnet", supports_reasoning=True,
        preserve_thinking_default=True,
    )
    return SimpleNamespace(
        thread_id=THREAD, target=target,
        boundary=SimpleNamespace(history_epoch=0, seq=9, message_id="m-9"),
        previous=None, projection=object(), previous_generation_id=None, working_dir="/tmp",
        reasoning_effort=None, binary=None, foldable_tokens=0,
    )


def _seed() -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
            "VALUES (?, 'user-1', 'codex/test', 0, 1, 't0', 't0')",
            (THREAD,),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_manual_no_claude_warm_source_builds_same_model_cold(isolated_db, monkeypatch):
    """A switched Claude thread needs no live session or registration to fold."""
    from core import checkpoint_agent_admission
    from core import checkpoint_render
    from history import projection
    from infra import db_async

    context = _claude_context()
    cold_request = replace(
        _request("manual"), mode="cold", source_harness="claude-code",
        source_model="claude-code/sonnet", writer_harness="claude-code",
        writer_model="claude-code/sonnet",
    )
    cold_provider = replace(
        _provider(), target=context.target, cold_source=object(),
    )
    calls = []

    async def run_inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def build_cold(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(request=cold_request, provider_request=cold_provider)

    monkeypatch.setattr(db_async, "run_in_db", run_inline)
    monkeypatch.setattr(
        checkpoint_agent_admission, "read_checkpoint_preparation",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        checkpoint_render, "capture_warm_checkpoint_source", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(projection, "slice_portable_projection", lambda value, **_kwargs: value)
    monkeypatch.setattr(checkpoint_render, "build_cold_checkpoint_admission", build_cold)

    request, provider = await checkpoint_agent_admission.prepare_external_checkpoint(
        THREAD, "user-1", trigger="manual",
    )

    assert request.mode == "cold"
    assert provider.cold_source is cold_provider.cold_source
    assert calls[0]["trigger"] == "manual"
    assert calls[0]["source_target"] is calls[0]["writer_target"] is context.target


@pytest.mark.asyncio
async def test_manual_checkpoint_stays_warm_when_source_is_available(isolated_db, monkeypatch):
    from core import checkpoint_agent_admission
    from core import checkpoint_render
    from infra import db_async

    context = _claude_context()
    warm_source = object()

    async def run_inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(db_async, "run_in_db", run_inline)
    monkeypatch.setattr(
        checkpoint_agent_admission, "read_checkpoint_preparation",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        checkpoint_render, "capture_warm_checkpoint_source", AsyncMock(return_value=warm_source),
    )
    monkeypatch.setattr(
        checkpoint_agent_admission, "_build_manual_cold_admission",
        lambda *_args: pytest.fail("warm manual checkpoint must not build cold"),
    )

    request, provider = await checkpoint_agent_admission.prepare_external_checkpoint(
        THREAD, "user-1", trigger="manual",
    )

    assert request.mode == "warm"
    assert provider.warm_source is warm_source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "cap_impossible", "reason"),
    [
        (None, False, "checkpoint_window_unknown"),
        (1_000, True, "checkpoint_configuration_impossible"),
        (200_000, False, "checkpoint_input_too_large"),
    ],
)
async def test_manual_cold_too_large_is_a_typed_409(
    isolated_db, monkeypatch, window, cap_impossible, reason,
):
    from fastapi import HTTPException

    from api import checkpoints
    from core import checkpoint_agent_admission
    from core import checkpoint_render
    from core.checkpoint_fit import ColdFoldInputTooLarge
    from infra import db_async, input_admission
    from llm import context_window

    context = _claude_context()

    async def run_inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def too_large(*_args):
        raise ColdFoldInputTooLarge("protected checkpoint input leaves no writer room")

    monkeypatch.setattr(checkpoints, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(checkpoints, "_owned_active", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(db_async, "run_in_db", run_inline)
    monkeypatch.setattr(
        checkpoint_agent_admission, "read_checkpoint_preparation",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        checkpoint_render, "capture_warm_checkpoint_source", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(checkpoint_agent_admission, "_build_manual_cold_admission", too_large)
    monkeypatch.setattr(
        context_window, "get_context_window_or_none", lambda *_args, **_kwargs: window,
    )
    monkeypatch.setattr(
        input_admission, "checkpoint_cap_impossible", lambda *_args: cap_impossible,
    )

    with pytest.raises(HTTPException) as raised:
        await checkpoints.request_manual_checkpoint(THREAD, token="t")

    assert raised.value.status_code == 409
    assert raised.value.detail["reason"] == reason
    model = context.target.catalog_id
    cap_message, cap_action = input_admission.checkpoint_cap_refusal(model)
    # Only a cap that fails before any history is blamed on the cap.
    assert (raised.value.detail["message"] == cap_message) is cap_impossible
    if window is not None:
        assert raised.value.detail["legal_action"] in raised.value.detail["message"]
        assert (raised.value.detail["legal_action"] == cap_action) is cap_impossible


@pytest.mark.asyncio
async def test_agent_checkpoint_without_warm_source_still_refuses(isolated_db, monkeypatch):
    from core import checkpoint_agent_admission
    from core import checkpoint_render
    from infra import db_async

    context = _claude_context()

    async def run_inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(db_async, "run_in_db", run_inline)
    monkeypatch.setattr(
        checkpoint_agent_admission, "read_checkpoint_preparation",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        checkpoint_render, "capture_warm_checkpoint_source", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        checkpoint_agent_admission, "_build_manual_cold_admission",
        lambda *_args: pytest.fail("agent checkpoint must not build cold"),
    )

    with pytest.raises(checkpoint_agent_admission.CheckpointNotReady) as raised:
        await checkpoint_agent_admission.prepare_external_checkpoint(
            THREAD, "user-1", trigger="agent",
        )

    assert raised.value.reason_key == "warm_capture_refused"


@pytest.mark.asyncio
async def test_agent_tool_keeps_warm_capture_refusal_message(isolated_db, monkeypatch):
    from core import checkpoint_agent_admission
    from tools import request_checkpoint

    _seed()
    emitted = []

    async def no_native():
        return None

    async def refuse(*_args, **_kwargs):
        raise checkpoint_agent_admission.CheckpointNotReady(
            "current provider continuity could not admit a warm checkpoint",
            reason_key="warm_capture_refused",
        )

    monkeypatch.setattr(request_checkpoint, "_emit", lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", no_native)
    monkeypatch.setattr(checkpoint_agent_admission, "prepare_external_checkpoint", refuse)

    result = await request_checkpoint.RequestCheckpointTool().execute(
        "boundary", _thread_id=THREAD,
    )

    assert result == "The provider session cannot be folded right now. Try after your next turn."
    assert emitted[0][0][1:3] == ("refused", "warm_capture_refused")


@pytest.mark.asyncio
async def test_agent_tool_starts_and_emits_distinct_started_reason(isolated_db, monkeypatch):
    from core import checkpoint_agent_admission
    from core import checkpoint_runtime
    from core.checkpoint_types import CheckpointRecord
    from tools import request_checkpoint

    _seed()
    request = _request()
    provider = _provider()
    emitted = []
    record = CheckpointRecord(
        generation_order=1, status="running", continuity_base_revision=0,
        continuity_base_snapshot_json="[]", raw_result=None,
        continuity_snapshot_json=None, artifact_snapshot_json=None,
        plan_status_snapshot_json=None, through_at=None, usage_json=None,
        error=None,
        captured_at=None, installed_at=None, ended_at=None, **asdict(request),
    )

    async def no_native():
        return None

    async def external(*_args, **_kwargs):
        return request, provider

    async def start(given, given_provider):
        assert (given, given_provider) == (request, provider)
        return record

    monkeypatch.setattr(request_checkpoint, "_emit", lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", no_native)
    monkeypatch.setattr(checkpoint_agent_admission, "prepare_external_checkpoint", external)
    monkeypatch.setattr(checkpoint_runtime, "request_checkpoint", start)

    result = await request_checkpoint.RequestCheckpointTool().execute(
        "phase complete", _thread_id=THREAD,
    )

    assert result.startswith("Checkpoint requested.")
    assert emitted == [
        ((THREAD, "started", "fired", "phase complete"), {
            "through_seq": 9, "writer_harness": "codex",
        })
    ]


@pytest.mark.asyncio
async def test_agent_tool_uses_native_admission_when_live_runtime_is_bound(monkeypatch):
    from core import checkpoint_runtime
    from core.checkpoint_resume import CheckpointAdmission
    from core.checkpoint_types import CheckpointRecord
    from tools import request_checkpoint

    request = _request()
    provider = _provider()
    record = CheckpointRecord(
        generation_order=1, status="running", continuity_base_revision=0,
        continuity_base_snapshot_json="[]", raw_result=None,
        continuity_snapshot_json=None, artifact_snapshot_json=None,
        plan_status_snapshot_json=None, through_at=None, usage_json=None,
        error=None,
        captured_at=None, installed_at=None, ended_at=None, **asdict(request),
    )

    async def native():
        return CheckpointAdmission(request, provider)

    async def start(given, given_provider):
        assert (given, given_provider) == (request, provider)
        return record

    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", native)
    monkeypatch.setattr(checkpoint_runtime, "request_checkpoint", start)
    monkeypatch.setattr(request_checkpoint, "_emit", lambda *_args, **_kwargs: None)

    result = await request_checkpoint.RequestCheckpointTool().execute(
        "native phase", _thread_id=THREAD,
    )

    assert "latest completed assistant step" in result


@pytest.mark.asyncio
async def test_agent_tool_refuses_below_minimum_with_measurement(isolated_db, monkeypatch):
    from core import checkpoint_agent_admission
    from tools import request_checkpoint

    _seed()
    emitted = []

    async def no_native():
        return None

    async def below(*_args, **_kwargs):
        raise checkpoint_agent_admission.CheckpointNotReady(
            "short", reason_key="below_minimum", foldable_tokens=12,
            threshold_tokens=32_768,
        )

    monkeypatch.setattr(request_checkpoint, "_emit", lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", no_native)
    monkeypatch.setattr(checkpoint_agent_admission, "prepare_external_checkpoint", below)

    result = await request_checkpoint.RequestCheckpointTool().execute(
        "small boundary", _thread_id=THREAD,
    )

    assert "12 of 32,768" in result
    assert emitted[0][0][1:3] == ("refused", "below_minimum")
    assert emitted[0][1] == {"foldable_tokens": 12, "threshold_tokens": 32_768}


@pytest.mark.asyncio
async def test_agent_tool_coalesces_one_live_checkpoint(isolated_db, monkeypatch):
    from core import checkpoint_agent_admission
    from core import checkpoint_runtime
    from core.checkpoint_types import CheckpointRecord
    from tools import request_checkpoint

    _seed()
    request = _request()
    provider = _provider()
    active = CheckpointRecord(
        generation_order=2, status="running", continuity_base_revision=0,
        continuity_base_snapshot_json="[]", raw_result=None,
        continuity_snapshot_json=None, artifact_snapshot_json=None,
        plan_status_snapshot_json=None, through_at=None, usage_json=None,
        error=None,
        captured_at=None, installed_at=None, ended_at=None,
        **asdict(replace(request, id="already-running")),
    )

    async def no_native():
        return None

    async def external(*_args, **_kwargs):
        return request, provider

    async def coalesce(*_args):
        return active

    monkeypatch.setattr("core.agent_checkpoint_runtime.prepare_native_agent_checkpoint", no_native)
    monkeypatch.setattr(checkpoint_agent_admission, "prepare_external_checkpoint", external)
    monkeypatch.setattr(checkpoint_runtime, "request_checkpoint", coalesce)

    result = await request_checkpoint.RequestCheckpointTool().execute(
        "same boundary", _thread_id=THREAD,
    )

    assert result == "A checkpoint is already in progress for this thread."


def test_catalog_exposes_agent_checkpoint_only_through_history_pack():
    from tools.catalog import TOOL_CATALOG, pack_members
    from tools.request_checkpoint import RequestCheckpointTool

    threshold = f"{IDLE_MIN_DELTA_TOKENS:,}"
    assert TOOL_CATALOG["request_checkpoint"].core is False
    assert "request_checkpoint" in pack_members()["history"]
    assert threshold in TOOL_CATALOG["request_checkpoint"].summary
    assert threshold in RequestCheckpointTool().description


@pytest.mark.asyncio
async def test_manual_endpoint_keeps_manual_trigger_without_minimum(monkeypatch):
    from api import checkpoints

    calls = []

    async def prepare(thread_id, user_id, *, trigger):
        calls.append((thread_id, user_id, trigger))
        return None

    monkeypatch.setattr(checkpoints, "prepare_external_checkpoint", prepare)

    assert await checkpoints._prepare_manual("thread", "user") is None
    assert calls == [("thread", "user", "manual")]
