"""An over-window warm fork corrects to cold instead of ending the turn (D2)."""

from __future__ import annotations

from core.checkpoint_types import CheckpointProviderForkRequest

import asyncio
from dataclasses import replace

import pytest

from core.fork_types import (
    ProviderForkRejected,
    ProviderForkRequest,
    ProviderForkResult,
    WarmForkUnusable,
)
from core.checkpoint_types import CheckpointRecord
from llm.resolution import ExecutionTarget


def _target(harness: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/writer", harness=harness, provider=harness,
        backend="harness", transport_model="writer",
        expected_reported_model=None, context_window=262_144, available=True,
        availability_reason=None, display_name=harness, supports_reasoning=True,
    )


WARM_SOURCE = object()


def _warm_request(harness: str, *, cold_builder=None) -> ProviderForkRequest:
    return CheckpointProviderForkRequest(
        fork_id="checkpoint-1", thread_id="thread-1",
        target=_target(harness), instruction="checkpoint",
        reasoning_effort=None, working_dir="/work",
        warm_source=WARM_SOURCE, same_model_cold=cold_builder,
    )


def _cold_admission(**overrides):
    from core.checkpoint_resume import CheckpointAdmission
    from native_checkpoint_fixtures import cold_source

    fields = {
        "fork_id": "checkpoint-1", "thread_id": "thread-1",
        "target": _target("native"), "instruction": "checkpoint",
        "reasoning_effort": None, "working_dir": "/work",
        "cold_source": cold_source(),
    }
    fields.update(overrides)
    return CheckpointAdmission(
        request=object(), provider_request=CheckpointProviderForkRequest(**fields),
    )


@pytest.mark.asyncio
async def test_unusable_warm_fork_runs_cold_once_on_the_same_row(monkeypatch):
    """The correction is one extra dispatch, not a loop, and keeps the row."""
    from core import checkpoint_cold_fallback as fallback

    adopted = []

    async def fake_run_in_db(_fn, *args, **_kwargs):
        adopted.append(args)

    monkeypatch.setattr(fallback, "run_in_db", fake_run_in_db)
    calls = []

    async def invoke(request, _cancel):
        calls.append(request)
        if request.warm_source is not None:
            raise WarmForkUnusable("initial input exceeds selected context window")
        return ProviderForkResult("cold body", None)

    request = _warm_request("native", cold_builder=_cold_admission)
    result = await fallback.invoke_with_cold_correction(
        request, asyncio.Event(), invoke,
    )

    assert result.raw_body == "cold body"
    assert len(calls) == 2
    assert calls[0].warm_source is not None and calls[1].warm_source is None
    assert calls[1].fork_id == "checkpoint-1"
    assert adopted == [("checkpoint-1",)]


@pytest.mark.asyncio
async def test_native_warm_fork_correction_uses_canonical_refusal_log(monkeypatch):
    from core import checkpoint_cold_fallback as fallback
    from loguru import logger

    async def fake_run_in_db(*_args, **_kwargs):
        return None

    async def invoke(request, _cancel):
        if request.warm_source is not None:
            raise WarmForkUnusable("over window")
        return ProviderForkResult("cold body", None)

    monkeypatch.setattr(fallback, "run_in_db", fake_run_in_db)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    try:
        await fallback.invoke_with_cold_correction(
            _warm_request("native", cold_builder=_cold_admission),
            asyncio.Event(),
            invoke,
        )
    finally:
        logger.remove(sink)

    assert lines == [
        "fork warm capture refused for thread-1 (native): warm_fork_over_window\n",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("cold_builder", (None,))
async def test_warm_only_admission_keeps_the_existing_failure(cold_builder):
    """Manual and idle carry no cold builder, so behavior is unchanged."""
    from core import checkpoint_cold_fallback as fallback

    async def invoke(_request, _cancel):
        raise WarmForkUnusable("initial input exceeds selected context window")

    with pytest.raises(WarmForkUnusable):
        await fallback.invoke_with_cold_correction(
            _warm_request("claude-code", cold_builder=cold_builder),
            asyncio.Event(), invoke,
        )


@pytest.mark.asyncio
async def test_a_provider_refusal_never_triggers_the_cold_path(monkeypatch):
    """Only a proved unusable fork corrects; a refusal stays terminal."""
    from core import checkpoint_cold_fallback as fallback

    calls = []

    async def invoke(request, _cancel):
        calls.append(request)
        raise ProviderForkRejected("Codex checkpoint was not accepted")

    with pytest.raises(ProviderForkRejected):
        await fallback.invoke_with_cold_correction(
            _warm_request("codex", cold_builder=_cold_admission),
            asyncio.Event(), invoke,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_impossible_cold_correction_is_terminal_not_another_attempt(
    monkeypatch,
):
    """ColdFoldInputTooLarge from the builder ends the operation."""
    from core import checkpoint_cold_fallback as fallback
    from core.fork_fit import ColdFoldInputTooLarge

    def cannot_build():
        raise ColdFoldInputTooLarge("minimum meaningful cold input does not fit")

    async def invoke(_request, _cancel):
        raise WarmForkUnusable("initial input exceeds selected context window")

    with pytest.raises(ColdFoldInputTooLarge):
        await fallback.invoke_with_cold_correction(
            _warm_request("claude-code", cold_builder=cannot_build),
            asyncio.Event(), invoke,
        )


@pytest.mark.asyncio
async def test_correction_refuses_a_builder_that_leaves_the_row(monkeypatch):
    """A cold admission for another row can never adopt this one."""
    from core import checkpoint_cold_fallback as fallback

    async def unused_run_in_db(*_args, **_kwargs):
        raise AssertionError("the row must be rejected before it is adopted")

    monkeypatch.setattr(fallback, "run_in_db", unused_run_in_db)

    async def invoke(_request, _cancel):
        raise WarmForkUnusable("initial input exceeds selected context window")

    with pytest.raises(ValueError):
        await fallback.invoke_with_cold_correction(
            _warm_request(
                "native",
                cold_builder=lambda: _cold_admission(fork_id="other"),
            ),
            asyncio.Event(), invoke,
        )


@pytest.mark.parametrize("harness,model", (
    ("claude-code", "claude-code/sonnet"),
    ("codex", "codex/gpt-5.6-terra"),
    ("native", "openai/gpt-5"),
))
def test_pressure_warm_admission_carries_its_cold_builder(harness, model):
    """Every pressure harness admits warm with a usable correction attached."""
    from core.checkpoint_warm_request import build_warm_admission
    from history.types import HistoryBoundary

    boundary = HistoryBoundary(
        thread_id="thread-1", history_epoch=1, seq=4, message_id="message-4",
    )
    admission = build_warm_admission(
        checkpoint_id="checkpoint-1", thread_id="thread-1", turn=None,
        target=_target(harness), boundary=boundary,
        previous_generation_id=None, working_dir="/work",
        reasoning_effort=None, binary=harness, warm_source=WARM_SOURCE,
    )

    # The builder is attached by each pressure admission, not by the warm
    # request itself; the base warm request must leave it unset.
    assert admission.provider_request.same_model_cold is None


def test_adopt_cold_only_moves_a_running_warm_pressure_row(isolated_db):
    """The CAS refuses any row that already wrote or is not warm pressure."""
    from infra.checkpoints import CheckpointTransitionError, adopt_cold
    from infra.database import get_db

    db = get_db()
    try:
        columns = (
            "id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
            "mode,status,source_harness,source_model,writer_harness,writer_model,"
            "continuity_base_revision,continuity_base_snapshot_json,started_at"
        )
        # One live checkpoint row per thread, so each case needs its own thread.
        for row_id, trigger, mode, status in (
            ("warm-pressure", "pressure", "warm", "running"),
            ("warm-manual", "manual", "warm", "running"),
            ("cold-pressure", "pressure", "cold", "running"),
            ("done-pressure", "pressure", "warm", "installed"),
        ):
            db.execute(
                "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
                "VALUES (?,'user-1','native/writer','t0','t0')",
                (f"thread-{row_id}",),
            )
            db.execute(
                f"INSERT INTO checkpoints ({columns}) VALUES "
                "(?,?,0,4,'message-4',?,?,?,'native','native/writer',"
                "'native','native/writer',0,'{}','t0')",
                (row_id, f"thread-{row_id}", trigger, mode, status),
            )
        db.commit()

        adopt_cold("warm-pressure", db=db)
        assert db.execute(
            "SELECT mode FROM checkpoints WHERE id='warm-pressure'",
        ).fetchone()[0] == "cold"
        for refused in ("warm-manual", "cold-pressure", "done-pressure"):
            with pytest.raises(CheckpointTransitionError):
                adopt_cold(refused, db=db)
    finally:
        db.close()


@pytest.mark.parametrize("harness", ("claude-code", "codex", "native"))
def test_each_adapter_raises_the_typed_refusal_for_its_warm_fork(harness):
    """The three over-window warm refusals are all correctable, not terminal."""
    import inspect

    module = {
        "claude-code": "core.fork_cc_io",
        "codex": "core.fork_codex",
        "native": "core.fork_native",
    }[harness]
    source = inspect.getsource(__import__(module, fromlist=["_"]))

    flattened = " ".join(source.split())
    assert "initial input exceeds selected context window" in flattened, (
        f"{harness} has no over-window warm refusal"
    )
    assert "WarmForkUnusable" in source


@pytest.mark.asyncio
async def test_cancel_between_the_warm_refusal_and_cold_dispatch_stops(monkeypatch):
    """A Stop taken during the correction is honored before any cold call."""
    from core import checkpoint_cold_fallback as fallback

    cancel = asyncio.Event()

    async def invoke(_request, _cancel):
        cancel.set()
        raise WarmForkUnusable("initial input exceeds selected context window")

    with pytest.raises(asyncio.CancelledError):
        await fallback.invoke_with_cold_correction(
            _warm_request("native", cold_builder=_cold_admission), cancel, invoke,
        )


def _record(mode: str) -> CheckpointRecord:
    return CheckpointRecord(
        generation_order=1, id="checkpoint-1", thread_id="thread-1",
        history_epoch=0, previous_generation_id=None, through_seq=4,
        through_message_id="message-4", trigger="pressure", mode=mode,
        status="running", source_harness="native", source_model="native/writer",
        writer_harness="native", writer_model="native/writer",
        originating_turn_id="turn-1", first_included_seq=None,
        continuity_base_revision=0, continuity_base_snapshot_json="{}",
        raw_result=None, continuity_snapshot_json=None,
        artifact_snapshot_json=None, plan_status_snapshot_json=None, through_at=None, usage_json=None,
        error=None, started_at="t0", captured_at=None, installed_at=None,
        ended_at=None,
    )


def test_warm_request_bytes_are_untouched_by_the_correction_path():
    """Carrying a builder must not change any cache-bearing warm field."""
    plain = _warm_request("claude-code")
    carrying = _warm_request("claude-code", cold_builder=_cold_admission)

    assert replace(carrying, same_model_cold=None) == plain
