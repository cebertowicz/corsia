"""A tool boundary reads checkpoint history once when a complete input count is given.

The first read only feeds ``_current_external_input``, which returns before using
it when ``complete_input_tokens`` is set. With a live measurement that read is
the checkpoint read, argument for argument, so it is skipped. Without a
measurement it is a seq-0 render that validates covered history, so it stays.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_external_boundary_after_checkpoint import (
    WINDOW,
    _decision,
    _inputs,
    _prepare,
)
from tests.test_history_slice_reads import LARGE, _settle_next_step, _target

COMPLETE = 5_000


async def _boundary(monkeypatch, harness, measured, complete_input_tokens):
    from core.checkpoint_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from core.turn_core import TurnIdentity
    from subagents import harness_checkpoint_resume as bridge

    captured: dict[str, Any] = {}
    reads: list[bool] = []
    with monkeypatch.context() as patch:
        patch.setattr(bridge, "external_measurement", lambda *_a: (measured, WINDOW))
        real = bridge.read_external_checkpoint_context

        def counted(*args, **kwargs):
            reads.append(kwargs.get("exact_history", False))
            return real(*args, **kwargs)

        patch.setattr(bridge, "read_external_checkpoint_context", counted)

        async def maintain(boundary, **options):
            captured["boundary"] = boundary
            return ProviderContinuation(BoundaryDecision.PROCEED, boundary.turn)

        patch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)
        live = SimpleNamespace(
            corsia_turn=TurnIdentity(f"{LARGE}-live", 1),
            checkpoint_wait_scope=CheckpointWaitScope(),
            catalog_model_id=_target(harness).catalog_id,
            session=SimpleNamespace(),
        )
        await bridge.maintain_external_tool_boundary(
            live, LARGE, harness, complete_input_tokens=complete_input_tokens,
        )
    return captured["boundary"], reads


HARNESSES = pytest.mark.parametrize("harness", ("claude-code", "codex"))


@pytest.mark.asyncio
@HARNESSES
async def test_complete_input_with_measurement_reads_history_once(
    isolated_db, monkeypatch, harness,
):
    from core.checkpoint_fit import BoundaryDecision

    _prepare(monkeypatch, harness)
    _settle_next_step(LARGE, 2)

    boundary, reads = await _boundary(monkeypatch, harness, 1_000, COMPLETE)

    assert _inputs(boundary) == (
        WINDOW, None, COMPLETE, 2115, f"{LARGE}-floor", None, f"{LARGE}-floor",
        True, f"{LARGE}-floor",
    )
    assert _decision(boundary) is BoundaryDecision.PROCEED
    assert reads == [False]


@pytest.mark.asyncio
@HARNESSES
async def test_without_complete_input_both_reads_remain(isolated_db, monkeypatch, harness):
    _prepare(monkeypatch, harness)
    _settle_next_step(LARGE, 2)

    _, reads = await _boundary(monkeypatch, harness, 1_000, None)

    assert reads == [False, False]


@pytest.mark.asyncio
@HARNESSES
async def test_without_measurement_the_seq0_read_still_validates_covered_history(
    isolated_db, monkeypatch, harness,
):
    from history.types import PortableHistoryError
    from infra.database import get_db

    _prepare(monkeypatch, harness)
    _settle_next_step(LARGE, 2)
    _, reads = await _boundary(monkeypatch, harness, None, COMPLETE)
    assert reads == [True, False]

    db = get_db()
    db.execute("UPDATE messages SET role='bogus' WHERE id=?", (f"{LARGE}-covered-0-user",))
    db.commit()
    db.close()

    # Covered history outside the checkpoint segment is invalid, and a measured
    # boundary never reads it, so only the unmeasured seq-0 read can fail here.
    _, measured_reads = await _boundary(monkeypatch, harness, 1_000, COMPLETE)
    assert measured_reads == [False]
    with pytest.raises(PortableHistoryError, match="unsupported role"):
        await _boundary(monkeypatch, harness, None, COMPLETE)
