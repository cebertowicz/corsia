"""Tool-boundary checkpoint decisions after reading history from the installed checkpoint.

Each case captures the ProviderBoundary built from the checkpoint-scoped read and
from a forced seq-0 read of the same thread. Inputs and decisions must match
wherever a newer legal cut exists; the one approved difference (#1588 D3) is
that, with no newer cut, live occupancy is no longer inflated by history the
installed checkpoint already covers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_history_slice_reads import LARGE, _seed, _settle_next_step, _target

WINDOW = 200_000


def _prepare(monkeypatch, harness: str, *, adopted: bool = True) -> None:
    """Seed the large thread and resolve its external target without discovery."""
    import tests.test_history_slice_reads as slice_reads

    target = _target(harness)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir", lambda *_a, **_k: "/tmp",
    )
    monkeypatch.setattr("core.checkpoint_render.checkpoint_binary", lambda *_a, **_k: None)
    if not adopted:
        monkeypatch.setattr(slice_reads, "_register_adopted_handle", lambda *_a: None)
    _seed(LARGE, target.catalog_id, harness)


def _move_floor_to_live_input(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "UPDATE checkpoints SET through_seq=(SELECT seq FROM messages WHERE id=?), "
        "through_message_id=? WHERE id=?",
        (f"{thread_id}-live-user", f"{thread_id}-live-user", f"{thread_id}-floor"),
    )
    db.commit()
    db.close()


async def _capture(monkeypatch, harness, measured, *, seq0: bool, must_adopt=False):
    from core.checkpoint_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from core.turn_core import TurnIdentity
    from subagents import harness_checkpoint_resume as bridge

    captured: dict[str, Any] = {}
    with monkeypatch.context() as patch:
        patch.setattr(bridge, "external_measurement", lambda *_a: (measured, WINDOW))
        if seq0:
            real = bridge.read_external_checkpoint_context
            patch.setattr(
                bridge, "read_external_checkpoint_context",
                lambda *args, **kwargs: real(*args, **{**kwargs, "exact_history": True}),
            )

        async def maintain(boundary, **options):
            captured.update(boundary=boundary, options=options)
            return ProviderContinuation(BoundaryDecision.PROCEED, boundary.turn)

        patch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)
        live = SimpleNamespace(
            corsia_turn=TurnIdentity(f"{LARGE}-live", 1),
            checkpoint_wait_scope=CheckpointWaitScope(),
            catalog_model_id=_target(harness).catalog_id,
            session=SimpleNamespace(),
        )
        await bridge.maintain_external_tool_boundary(live, LARGE, harness, must_adopt=must_adopt)
    return captured["boundary"], captured["options"]


def _inputs(boundary) -> tuple:
    return (
        boundary.context_window, boundary.canonical_input_tokens,
        boundary.provider_input_tokens, boundary.minimum_input_tokens,
        boundary.adopted_checkpoint_id, getattr(boundary.active_checkpoint, "id", None),
        getattr(boundary.installed_checkpoint, "id", None),
        boundary.prepare_checkpoint is not None, boundary.eager_checkpoint_id,
    )


def _decision(boundary):
    from core.checkpoint_fit import evaluate_boundary
    from core.checkpoint_resume import InstalledContinuation, _payload

    installed = (
        InstalledContinuation(10_000, None) if boundary.installed_checkpoint else None
    )
    return evaluate_boundary(_payload(boundary, boundary.active_checkpoint, installed))


HARNESSES = pytest.mark.parametrize("harness", ("claude-code", "codex"))


@pytest.mark.asyncio
@HARNESSES
async def test_newer_legal_cut_keeps_inputs_and_decisions_identical(
    isolated_db, monkeypatch, harness,
):
    _prepare(monkeypatch, harness)
    _settle_next_step(LARGE, 2)
    scoped, _ = await _capture(monkeypatch, harness, 1_000, seq0=False)
    tail = scoped.provider_input_tokens - 1_000
    assert scoped.prepare_checkpoint is not None

    for measured in (1_000, WINDOW - tail, WINDOW - tail + 1, WINDOW + 5_000):
        scoped, _ = await _capture(monkeypatch, harness, measured, seq0=False)
        seq0, _ = await _capture(monkeypatch, harness, measured, seq0=True)
        assert _inputs(scoped) == _inputs(seq0), measured
        assert _decision(scoped) == _decision(seq0), measured


@pytest.mark.asyncio
@HARNESSES
async def test_without_newer_cut_occupancy_counts_only_history_after_the_checkpoint(
    isolated_db, monkeypatch, harness,
):
    from core.checkpoint_fit import BoundaryDecision

    _prepare(monkeypatch, harness)
    _move_floor_to_live_input(LARGE)
    measured = WINDOW - 20_000

    scoped, _ = await _capture(monkeypatch, harness, measured, seq0=False)
    seq0, _ = await _capture(monkeypatch, harness, measured, seq0=True)

    assert scoped.prepare_checkpoint is None and seq0.prepare_checkpoint is None
    assert scoped.provider_input_tokens < WINDOW < seq0.provider_input_tokens
    assert _decision(scoped) in {BoundaryDecision.PROCEED, BoundaryDecision.PROCEED_AND_FOLD}
    assert _decision(seq0) is BoundaryDecision.ADOPT_AND_PROCEED


@pytest.mark.asyncio
@HARNESSES
async def test_live_session_over_window_without_newer_cut_stays_over_window(
    isolated_db, monkeypatch, harness,
):
    _prepare(monkeypatch, harness)
    _move_floor_to_live_input(LARGE)

    scoped, _ = await _capture(monkeypatch, harness, WINDOW + 10_000, seq0=False)
    seq0, _ = await _capture(monkeypatch, harness, WINDOW + 10_000, seq0=True)

    assert scoped.provider_input_tokens > WINDOW
    assert _decision(scoped) == _decision(seq0)


@pytest.mark.asyncio
@HARNESSES
async def test_unadopted_eager_checkpoint_still_requires_adoption(
    isolated_db, monkeypatch, harness,
):
    _prepare(monkeypatch, harness, adopted=False)
    _settle_next_step(LARGE, 2)

    scoped, _ = await _capture(monkeypatch, harness, 1_000, seq0=False)
    seq0, _ = await _capture(monkeypatch, harness, 1_000, seq0=True)

    assert scoped.eager_checkpoint_id == f"{LARGE}-floor"
    assert scoped.adopted_checkpoint_id is None
    assert _inputs(scoped) == _inputs(seq0)


@pytest.mark.asyncio
@HARNESSES
async def test_mandatory_adoption_passes_through_unchanged(isolated_db, monkeypatch, harness):
    _prepare(monkeypatch, harness)
    _settle_next_step(LARGE, 2)

    scoped, options = await _capture(monkeypatch, harness, 1_000, seq0=False, must_adopt=True)
    seq0, seq0_options = await _capture(monkeypatch, harness, 1_000, seq0=True, must_adopt=True)

    assert options == seq0_options == {"must_adopt": True}
    assert _inputs(scoped) == _inputs(seq0)
