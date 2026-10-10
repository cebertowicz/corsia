"""Unavailable maintenance cannot consume a proved-unwritten FIFO input."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.checkpoint_fit import BoundaryDecision, ColdFoldInputTooLarge
from core.checkpoint_resume import (
    CheckpointContinuationError,
    ProviderBoundary,
    maintain_before_provider_call,
)
from core.checkpoint_types import CheckpointRecord
from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TurnIdentity


def _boundary(turn: TurnIdentity, *, tokens: int, prepare=None, active=None):
    async def no_installed_context(_record):
        raise AssertionError("no installed checkpoint exists in this case")

    return ProviderBoundary(
        thread_id="thread-unavailable", turn=turn, context_window=100_000,
        canonical_input_tokens=tokens, provider_input_tokens=tokens,
        minimum_input_tokens=1_000, adopted_checkpoint_id=None,
        active_checkpoint=active, installed_checkpoint=None,
        prepare_checkpoint=prepare, render_installed=no_installed_context,
        wait_scope=CheckpointWaitScope(),
    )


@pytest.mark.asyncio
async def test_optional_checkpoint_rejection_preserves_fitting_continuation():
    """A protected-source fit failure is not a foreground admission failure."""
    async def cannot_fit(_checkpoint_id, required):
        assert required is False
        raise ColdFoldInputTooLarge("protected checkpoint source cannot fit")

    boundary = _boundary(
        TurnIdentity("turn-unavailable", 1), tokens=95_000, prepare=cannot_fit,
    )

    continuation = await maintain_before_provider_call(boundary)

    assert continuation.decision is BoundaryDecision.PROCEED
    assert continuation.turn == boundary.turn


def _checkpoint(turn: TurnIdentity) -> CheckpointRecord:
    return CheckpointRecord(
        generation_order=1, id="checkpoint-unavailable",
        thread_id="thread-unavailable", history_epoch=0,
        previous_generation_id=None, through_seq=1,
        through_message_id="prior-message", trigger="pressure", mode="cold",
        status="running", source_harness="claude-code",
        source_model="claude-code/sonnet", writer_harness="claude-code",
        writer_model="claude-code/sonnet", originating_turn_id=turn.turn_id,
        first_included_seq=1, continuity_base_revision=0,
        continuity_base_snapshot_json="{}", raw_result=None,
        continuity_snapshot_json=None, artifact_snapshot_json=None,
        plan_status_snapshot_json=None, through_at=None, usage_json=None,
        error=None,
        started_at="2026-09-12T13:00:00+00:00", captured_at=None,
        installed_at=None, ended_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", (
    "oMLX prefill memory guard rejected prompt (kv_len=0)",
    "protected checkpoint source cannot fit",
    "Claude cold context admission timed out",
))
async def test_required_checkpoint_failure_fails_fenced_input_visibly(
    isolated_db, monkeypatch, reason,
):
    """The same accepted row keeps its identity and its typed no-write evidence.

    A deterministic refusal has no reachable release, so the row is settled as a
    visible failure rather than conserved; nothing about it is rewritten, resent
    or duplicated.
    """
    from infra.database import get_db
    from infra.input_writes import dispatch_fenced_batch
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-unavailable','user-1','claude-code/sonnet','t0','t0')"
        )
        db.commit()
        admission = admit_input(
            "thread-unavailable", role="user", content="preserve this input",
            intent="apply_now", message_id="input-unavailable",
            turn_id="turn-unavailable", boot_epoch="test-boot",
            admitted_at="t1", db=db,
        )
        assert admission.batch is not None
        batch = admission.batch
        active = _checkpoint(batch.identity)
        original = db.execute(
            "SELECT id,input_order,content,delivery_state,seq FROM messages "
            "WHERE id='input-unavailable'"
        ).fetchone()
        assert (original["delivery_state"], original["seq"]) == ("queued", None)

        async def completed(checkpoint_id):
            assert checkpoint_id == active.id
            return replace(active, status="failed", error=reason,
                           ended_at="2026-09-12T13:01:00+00:00")

        monkeypatch.setattr("core.checkpoint_runtime.await_checkpoint", completed)
        boundary = replace(
            _boundary(batch.identity, tokens=200_001, active=active),
            context_window=200_000,
        )

        async def dispatch(_evidence):
            await maintain_before_provider_call(boundary)
            raise AssertionError("foreground provider must not be called")

        try:
            await dispatch_fenced_batch(
                batch, dispatch, attempt_id="attempt-unavailable",
                started_at="t2", db=db,
            )
        except CheckpointContinuationError:
            pass

        row = db.execute(
            "SELECT id,input_order,content,delivery_state,seq FROM messages "
            "WHERE id='input-unavailable'"
        ).fetchone()
        assert tuple(row)[:3] == tuple(original)[:3]  # Identity and text survive.
        assert row["delivery_state"] == "failed"
        assert row["seq"] is not None  # The failure takes its place in the transcript.
        assert tuple(db.execute(
            "SELECT write_outcome,active_turn_id FROM messages WHERE id='input-unavailable'",
        ).fetchone()) == ("proved_no_write", "turn-unavailable")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unavailable_after_install_carries_the_checkpoint_just_tested(monkeypatch):
    """An unusable new installation must not look like recovery at settlement."""
    from core.checkpoint_resume import InstalledContinuation
    from core.checkpoint_types import CheckpointUnavailable

    turn = TurnIdentity("turn-unavailable", 1)
    active = _checkpoint(turn)
    installed = replace(active, status="installed")

    async def completed(checkpoint_id):
        assert checkpoint_id == active.id
        return installed

    async def too_large(record):
        assert record.id == installed.id
        return InstalledContinuation(100_001, "protected context plus tail")

    monkeypatch.setattr("core.checkpoint_runtime.await_checkpoint", completed)
    boundary = replace(
        _boundary(turn, tokens=100_001, active=active), render_installed=too_large,
    )
    with pytest.raises(CheckpointUnavailable) as caught:
        await maintain_before_provider_call(boundary)
    assert caught.value.installed_checkpoint_id == installed.id


def test_terminal_wake_does_not_reclaim_proved_unwritten_queued_input(isolated_db):
    """Retiring the failed Turn must not immediately dispatch the held prefix."""
    from infra.database import get_db
    from infra.thread_driver import admit_input, finish_turn
    from infra.thread_inputs import start_idle_turn

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-held','user-1','claude-code/sonnet','t0','t0')"
        )
        db.commit()
        admitted = admit_input(
            "thread-held", role="user", content="keep this queued",
            intent="apply_now", message_id="input-held", turn_id="turn-held",
            boot_epoch="test-boot", admitted_at="t1", db=db,
        )
        assert admitted.batch is not None
        # The adapter has proved no foreground write and released ownership;
        # the terminal transaction and subsequent wakes must respect that hold.
        db.execute(
            "UPDATE messages SET active_turn_id=NULL,write_outcome='proved_no_write',"
            "delivery_error='checkpoint unavailable' WHERE id='input-held'"
        )
        db.commit()

        terminal = finish_turn(
            "turn-held", outcome="error", ended_at="t2",
            successor_turn_id="must-not-start", boot_epoch="test-boot", db=db,
        )
        assert terminal.successor is None
        for index in range(3):
            assert start_idle_turn(
                "thread-held", turn_id=f"wake-{index}", boot_epoch="test-boot",
                started_at="t3", db=db,
            ) is None
        row = db.execute(
            "SELECT content,delivery_state,seq,active_turn_id FROM messages "
            "WHERE id='input-held'"
        ).fetchone()
        assert tuple(row) == ("keep this queued", "queued", None, None)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced", (False, True))
async def test_native_terminal_keeps_typed_no_write_evidence(
    isolated_db, monkeypatch, fenced,
):
    """The Native owner must retain typed no-write evidence through retirement."""
    import asyncio
    from types import SimpleNamespace
    from core import native_turn_runtime as runtime
    from core.checkpoint_types import CheckpointUnavailable
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-native-held','user-1','local/test','t0','t0')"
        )
        db.commit()
        admission = admit_input(
            "thread-native-held", role="user", content="keep native input",
            intent="apply_now", message_id="native-input", turn_id="native-turn",
            boot_epoch="test-boot", admitted_at="t1", db=db,
        )
        assert admission.batch is not None
        if fenced:
            begin_batch_write(
                admission.batch, attempt_id="native-attempt", started_at="t2", db=db,
            )

        failure = CheckpointUnavailable(
            "checkpoint unavailable", context_window=100_000,
            installed_checkpoint_id=None,
        )

        async def reject_provider(**_kwargs):
            raise failure

        async def settle(outcome, error):
            assert outcome == "error"
            settle_batch_write(
                "native-attempt", outcome="proved_no_write", error=error, db=db,
            )

        monkeypatch.setattr(runtime, "run_turn_with_steps", reject_provider)
        turn = runtime.NativeTurn(
            agent=SimpleNamespace(), thread_id="thread-native-held",
            content="keep native input", model="local/test", turn_provider={},
            handler=SimpleNamespace(), cancel_event=asyncio.Event(),
            identity=admission.batch.identity,
            on_provider_terminal=settle if fenced else None,
        )
        with pytest.raises(CheckpointUnavailable):
            await runtime.run_tracked_turn(turn)
        row = db.execute(
            "SELECT delivery_state,seq,write_outcome,active_turn_id FROM messages "
            "WHERE id='native-input'"
        ).fetchone()
        # Settled exactly like any other visible failure of this Turn.
        assert (row["delivery_state"], row["write_outcome"], row["active_turn_id"]) == (
            "failed", "proved_no_write", "native-turn",
        )
        assert row["seq"] is not None
        assert tuple(db.execute(
            "SELECT status,error FROM active_turns WHERE id='native-turn'",
        ).fetchone()) == ("error", "checkpoint unavailable")
    finally:
        db.close()
