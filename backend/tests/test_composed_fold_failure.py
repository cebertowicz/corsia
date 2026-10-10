"""An impossible fold fails its own input, and the thread keeps answering.

Composed for all three harnesses, across the seam the wedge actually crossed:
the cold admission each writer family measures for itself (D1), the boundary
that converts its refusal into an input settlement (D2), and the FIFO that
settlement releases (D3). The specimen wedged because the refusal stopped at
the input and the thread went silent; here the refused input fails visibly and
the input behind it is answered.
"""

from __future__ import annotations

import pytest

from core.checkpoint_fit import ColdFoldInputTooLarge
from core.checkpoint_resume import ProviderBoundary, maintain_before_provider_call
from core.checkpoint_types import CheckpointUnavailable
from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TurnIdentity
from infra.database import get_db
from infra.input_writes import begin_batch_write, settle_batch_write
from infra.thread_driver import admit_input, finish_turn
from llm.resolution import ExecutionTarget
from native_checkpoint_fixtures import cold_source

HARNESSES = ("native", "codex", "claude-code")

WINDOW = 262_144


def _target(harness: str, *, context_window: int) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/writer", harness=harness, provider=harness,
        backend="harness", transport_model=f"model-{harness}",
        expected_reported_model=None, context_window=context_window,
        available=True, availability_reason=None, display_name=harness,
        supports_reasoning=True,
    )


def _cold_admission(monkeypatch, harness: str, *, window: int = WINDOW):
    """Build the real cold admission this writer would send."""
    from core.checkpoint_render import build_cold_checkpoint_admission

    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_a: {})
    monkeypatch.setattr(
        "prompts.checkpoint.build_checkpoint_system_prompt",
        lambda: "checkpoint instruction",
    )
    target = _target(harness, context_window=window)
    projection = cold_source().projection
    return build_cold_checkpoint_admission(
        checkpoint_id="checkpoint-1", thread_id="held-thread",
        trigger="pressure", source_target=target, writer_target=target,
        boundary=projection.sealed_boundary, previous=None,
        projection=projection, working_dir="/work/project",
        reasoning_effort="high", binary=harness, db=object(),
    )


def _surface_that_just_fits(request) -> int:
    """The largest loaded writer surface this source still folds under."""
    from core.checkpoint_render import fit_cold_checkpoint_input

    allowance = WINDOW - request.max_output_tokens - WINDOW // 20
    baseline = fit_cold_checkpoint_input(request, provider_surface_tokens=0)
    return allowance - baseline.input_tokens


def _refuse_the_fold(request) -> ColdFoldInputTooLarge:
    """Refuse this exact fold the way its own writer's surface would."""
    from core.checkpoint_render import fit_cold_checkpoint_input

    with pytest.raises(ColdFoldInputTooLarge) as refusal:
        fit_cold_checkpoint_input(
            request, provider_surface_tokens=_surface_that_just_fits(request) + 1,
        )
    return refusal.value


def _boundary(prepare, *, window: int) -> ProviderBoundary:
    async def no_installed_context(_record):
        raise AssertionError("this case never renders an installed checkpoint")

    # Over the window with a fitting minimum: the fold is required, which is
    # the only state where a refusal has to reach the input.
    return ProviderBoundary(
        thread_id="held-thread", turn=TurnIdentity("held-turn", 1),
        context_window=window, canonical_input_tokens=window + 1,
        provider_input_tokens=window + 1, minimum_input_tokens=1_000,
        adopted_checkpoint_id=None, active_checkpoint=None,
        installed_checkpoint=None, prepare_checkpoint=prepare,
        render_installed=no_installed_context, wait_scope=CheckpointWaitScope(),
    )


@pytest.fixture
def wedged_thread(isolated_db):
    """The specimen shape: one input in flight, one queued behind it."""
    db = get_db()
    db.execute(
        "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) "
        "VALUES ('local/held','local','held','local/held','Held',262144,'t0','t0')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('held-thread','user-1','local/held','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "held-thread", role="user", content="first ask", intent="apply_now",
        message_id="first-input", turn_id="held-turn", boot_epoch="boot",
        admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id="held-attempt", started_at="t2", db=db)
    admit_input(
        "held-thread", role="user", content="second ask", intent="apply_now",
        message_id="second-input", turn_id="queued-turn", boot_epoch="boot",
        admitted_at="t3", db=db,
    )
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("harness", HARNESSES)
def test_each_writer_refuses_only_the_fold_its_surface_cannot_hold(
    monkeypatch, harness: str,
) -> None:
    """The refusal is decided per writer family, before any provider call."""
    from core.checkpoint_render import fit_cold_checkpoint_input

    request = _cold_admission(monkeypatch, harness).provider_request
    surface = _surface_that_just_fits(request)

    fitted = fit_cold_checkpoint_input(request, provider_surface_tokens=surface)
    refusal = _refuse_the_fold(request)

    # One token apart: the boundary is the measured surface, not a guess.
    assert fitted.first_included_seq is not None
    assert "does not fit" in str(refusal)


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.asyncio
async def test_an_impossible_fold_fails_its_input_and_frees_the_fifo(
    monkeypatch, wedged_thread, harness: str,
) -> None:
    """One refused fold must cost exactly one input, not the whole thread."""
    db = wedged_thread

    request = _cold_admission(monkeypatch, harness).provider_request
    refusal = _refuse_the_fold(request)

    async def prepare(_checkpoint_id, _required):
        raise refusal

    with pytest.raises(CheckpointUnavailable) as unavailable:
        await maintain_before_provider_call(_boundary(prepare, window=WINDOW))

    settle_batch_write(
        "held-attempt", outcome="proved_no_write", db=db, error=unavailable.value,
    )

    refused = db.execute(
        "SELECT delivery_state,write_outcome,delivery_error,seq FROM messages "
        "WHERE id='first-input'",
    ).fetchone()
    assert refused["delivery_state"] == "failed"  # Visible, with a reason.
    assert refused["write_outcome"] == "proved_no_write"
    assert "does not fit" in refused["delivery_error"]
    assert refused["seq"] is not None

    terminal = finish_turn(
        "held-turn", outcome="error", ended_at="t4",
        successor_turn_id="queued-turn", boot_epoch="boot", db=db,
    )

    assert terminal.successor is not None
    claimed = [item.message_id for item in terminal.successor.items]
    assert claimed == ["second-input"]  # Answered, and the failure is settled.


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.asyncio
async def test_a_possible_fold_leaves_the_whole_fifo_deliverable(
    monkeypatch, wedged_thread, harness: str,
) -> None:
    """Once the fold fits, nothing in the queue is left behind or reordered."""
    db = wedged_thread
    admission = _cold_admission(monkeypatch, harness)
    assert admission.provider_request.target.harness == harness

    settle_batch_write("held-attempt", outcome="accepted", db=db)
    terminal = finish_turn(
        "held-turn", outcome="completed", ended_at="t4",
        successor_turn_id="queued-turn", boot_epoch="boot", db=db,
    )

    assert terminal.successor is not None
    assert [item.message_id for item in terminal.successor.items] == ["second-input"]
    assert [
        row["id"] for row in db.execute(
            "SELECT id FROM messages WHERE thread_id='held-thread' "
            "AND role='user' ORDER BY input_order",
        )
    ] == ["first-input", "second-input"]
