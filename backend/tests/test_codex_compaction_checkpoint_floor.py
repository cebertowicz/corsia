"""Codex compaction must find a legal replacement checkpoint boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


THREAD = "codex-compaction-floor"
TURN = "codex-running-turn"
MODEL = "codex/gpt-5-codex"


def _insert_history_with_checkpoint_floor() -> None:
    """Install at the current Turn's input, leaving one completed Step above it."""
    from core.checkpoint_types import CheckpointRequest
    from infra.checkpoints import capture, create_running, install
    from infra.database import get_db
    from registry.service import register_codex_thread

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,next_message_seq,created_at,updated_at) "
            "VALUES (?, 'steve', 'Compaction floor', ?, 4, 't0', 't0')",
            (THREAD, MODEL),
        )
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,boot_epoch) VALUES (?,?,0,?,'running','t0','boot')",
            (TURN, THREAD, MODEL),
        )
        rows = (
            ("m1", "user", "continue", {"turn_id": TURN}),
            (
                "m2", "assistant", "step",
                {
                    "turn_id": TURN,
                    "llm_tool_calls": [{
                        "id": "call-1", "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }],
                },
            ),
            (
                "m3", "tool", "result",
                {
                    "turn_id": TURN, "tool_call_id": "call-1",
                    "assistant_step_id": "m2", "name": "Read",
                },
            ),
        )
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,"
            "active_turn_id) VALUES (?,?,?,?,?,'t0',?,?)",
            [
                (message_id, THREAD, role, content, json.dumps(metadata), index, TURN)
                for index, (message_id, role, content, metadata) in enumerate(rows, 1)
            ],
        )
        request = CheckpointRequest(
            id="checkpoint-floor", thread_id=THREAD, history_epoch=0,
            previous_generation_id=None, through_seq=1, through_message_id="m1",
            trigger="pressure", mode="cold", source_harness="codex", source_model=MODEL,
            writer_harness="codex", writer_model=MODEL, originating_turn_id=TURN,
            first_included_seq=1, started_at="t1",
        )
        create_running(request, db=db)
        capture("checkpoint-floor", raw_result="checkpoint", usage_json="{}", captured_at="t2", db=db)
        install("checkpoint-floor", installed_at="t3", db=db)
        register_codex_thread(
            THREAD, "provider-thread", checkpoint_generation_id="checkpoint-floor", db=db,
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_codex_compaction_after_same_turn_checkpoint_has_no_new_legal_boundary(
    isolated_db, monkeypatch,
):
    """This is the production shape: the only tail is a completed Step in the running Turn."""
    from core.checkpoint_resume import CheckpointWaitScope
    from core.checkpoint_types import CheckpointUnavailable
    from core.turn_core import TurnIdentity
    from subagents.harness_checkpoint_context import read_external_checkpoint_context
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    _insert_history_with_checkpoint_floor()

    context = read_external_checkpoint_context(THREAD, MODEL, "codex")
    assert context is not None
    # The floor is the only legal cut before m2, and the boundary selector
    # rejects a candidate at or below that installed checkpoint.
    assert context.boundary is None

    # A provider registration says the active handle already adopted the floor.
    # With no live usage after compaction, maintain selects its exact replay but
    # must not offer that same checkpoint as the replacement candidate.
    async def render_replay(*_args, **_kwargs):
        return SimpleNamespace(
            rendered=SimpleNamespace(input_tokens=1),
            fit_ledger=SimpleNamespace(section_tokens=()),
        )

    resumed: list[object] = []

    async def resume(_live, _thread_id, prepared, _harness):
        resumed.append(prepared)

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.render_external_selection", render_replay,
    )
    monkeypatch.setattr("subagents.harness_checkpoint_resume._resume_external", resume)
    live = SimpleNamespace(
        catalog_model_id=MODEL,
        corsia_turn=TurnIdentity(TURN, 0),
        checkpoint_wait_scope=CheckpointWaitScope(),
        context_window=200_000,
        session=SimpleNamespace(peek_active_usage=lambda: None),
    )
    assert await maintain_external_tool_boundary(
        live, THREAD, "codex", must_adopt=True,
    )
    assert len(resumed) == 1


@pytest.mark.asyncio
async def test_pressure_launch_does_not_start_a_second_fold_while_one_is_active():
    """`evaluate_boundary` suppresses a duplicate optional fold while active."""
    from core.fork_fit import BoundaryDecision, soft_pressure_remaining
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        ProviderBoundary,
        maintain_before_provider_call,
    )
    from core.turn_core import TurnIdentity

    window = 200_000
    calls: list[bool] = []

    async def prepare(_checkpoint_id: str, required: bool):
        calls.append(required)
        raise AssertionError("an active checkpoint must prevent another admission")

    launch_tokens = window - soft_pressure_remaining(window)
    boundary = ProviderBoundary(
        thread_id=THREAD, turn=TurnIdentity(TURN, 0), context_window=window,
        canonical_input_tokens=launch_tokens, provider_input_tokens=launch_tokens,
        minimum_input_tokens=1, adopted_checkpoint_id=None,
        active_checkpoint=SimpleNamespace(status="running"), installed_checkpoint=None,
        prepare_checkpoint=prepare, render_installed=lambda _record: None,
        wait_scope=CheckpointWaitScope(),
    )

    continuation = await maintain_before_provider_call(boundary)
    assert continuation.decision is BoundaryDecision.PROCEED
    assert calls == []


@pytest.mark.asyncio
async def test_compaction_keeps_the_visible_failure_when_registered_tail_does_not_fit(
    isolated_db, monkeypatch,
):
    from core.checkpoint_resume import CheckpointWaitScope
    from core.checkpoint_types import CheckpointUnavailable
    from core.turn_core import TurnIdentity
    from subagents.harness_checkpoint_context import read_external_checkpoint_context
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    _insert_history_with_checkpoint_floor()
    context = read_external_checkpoint_context(THREAD, MODEL, "codex")
    assert context is not None and context.boundary is None

    rendered: list[str] = []

    async def render_overflow(*args, **_kwargs):
        rendered.append(args[3])
        return SimpleNamespace(
            rendered=SimpleNamespace(input_tokens=200_001),
            fit_ledger=SimpleNamespace(section_tokens=()),
        )

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.render_external_selection", render_overflow,
    )
    live = SimpleNamespace(
        catalog_model_id=MODEL,
        corsia_turn=TurnIdentity(TURN, 0),
        checkpoint_wait_scope=CheckpointWaitScope(),
        context_window=200_000,
        session=SimpleNamespace(peek_active_usage=lambda: None),
    )

    with pytest.raises(
        CheckpointUnavailable,
        match="the next provider call does not fit and no checkpoint is admissible",
    ):
        await maintain_external_tool_boundary(live, THREAD, "codex", must_adopt=True)
    # One render determines current input; a second proves the registered
    # installed continuation was considered and found too large.
    assert sum(label.startswith("tool-boundary:") for label in rendered) == 1
    assert sum(label.startswith("checkpoint:") for label in rendered) == 1
    # Recovery pressure measures the installed replay independently.
    assert sum(label.startswith("recovery:") for label in rendered) == 1
    assert len(rendered) == 3
