"""Codex recovery pressure requests a fold without blocking a fitting call."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_codex_compaction_checkpoint_floor import MODEL, THREAD, TURN, _insert_history_with_checkpoint_floor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "estimate", "rendered", "expected_calls", "expected_decision"),
    [
        ("estimate", 233_472, 1, 0, "proceed_and_fold"),
        ("render", 1, 233_472, 1, "proceed_and_fold"),
        ("active", 1, 233_472, 0, "proceed"),
        ("other", 1, 233_472, 0, "proceed"),
        ("no_checkpoint", 1, 233_472, 0, "proceed"),
    ],
)
async def test_recovery_pressure_at_tool_boundary(
    isolated_db, monkeypatch, case, estimate, rendered, expected_calls, expected_decision,
):
    from core import checkpoint_resume, fork_fit
    from core.checkpoint_resume import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import harness_checkpoint_resume as bridge
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    _insert_history_with_checkpoint_floor()
    context = read_external_checkpoint_context(THREAD, MODEL, "codex", current_turn_id=TURN)
    assert context is not None and context.installed_checkpoint is not None
    target = replace(context.target, context_window=262_144, harness="claude-code" if case == "other" else "codex")
    context = replace(
        context, target=target, boundary=object(),
        active_checkpoint=SimpleNamespace(status="running") if case == "active" else None,
        installed_checkpoint=None if case == "no_checkpoint" else context.installed_checkpoint,
    )
    async def contexts(*_args):
        return context, context

    async def current(*_args, **_kwargs):
        return 100, None, 262_144

    calls = []
    async def render(*_args):
        calls.append(1)
        return SimpleNamespace(rendered=SimpleNamespace(input_tokens=rendered))

    decisions = []
    async def maintain(boundary, **_kwargs):
        decision = fork_fit.evaluate_boundary(checkpoint_resume._payload(boundary, boundary.active_checkpoint, None))
        decisions.append((decision.value, boundary.recovery_input_tokens))
        return SimpleNamespace(payload=None)

    monkeypatch.setattr(bridge, "read_boundary_contexts", contexts)
    monkeypatch.setattr(bridge, "_current_external_input", current)
    monkeypatch.setattr(bridge, "render_external_selection", render)
    monkeypatch.setattr(checkpoint_resume, "maintain_before_provider_call", maintain)
    monkeypatch.setattr(fork_fit, "foldable_tail_tokens", lambda *_args, **_kwargs: estimate)
    live = SimpleNamespace(
        catalog_model_id=MODEL, corsia_turn=TurnIdentity(TURN, 0),
        checkpoint_wait_scope=CheckpointWaitScope(), context_window=262_144,
        session=SimpleNamespace(peek_active_usage=lambda: None),
    )
    await bridge.maintain_external_tool_boundary(live, THREAD, target.harness, complete_input_tokens=100)
    assert len(calls) == expected_calls
    assert decisions[0][0] == expected_decision
    if case in {"other", "no_checkpoint", "active"}:
        assert decisions[0][1] is None


def test_recovery_pressure_does_not_change_the_hard_fit_wall():
    from core.fork_fit import BoundaryDecision, ProviderPayload, evaluate_boundary

    payload = ProviderPayload(
        context_window=200_000, canonical_input_tokens=100_000, input_tokens=100_000,
        minimum_tokens=1_000, checkpoint_status=None, installed_input_tokens=None,
        checkpoint_admissible=True, recovery_input_tokens=300_000,
    )
    assert evaluate_boundary(payload) == BoundaryDecision.PROCEED_AND_FOLD
    assert evaluate_boundary(replace(payload, input_tokens=200_001)) == BoundaryDecision.WAIT
    assert evaluate_boundary(replace(payload, input_tokens=200_001, minimum_tokens=200_001)) == BoundaryDecision.FAIL_VISIBLE


@pytest.mark.asyncio
async def test_large_tail_uses_estimate_without_render(isolated_db):
    import json
    from core.turn_core import TurnIdentity
    from infra.database import get_db
    from subagents.harness_checkpoint_context import read_external_checkpoint_context
    from subagents.harness_checkpoint_measure import recovery_input_tokens

    _insert_history_with_checkpoint_floor()
    db = get_db()
    rows = []
    for i in range(6800):
        step, call = f"pressure-step-{i}", f"pressure-call-{i}"
        sm = {"turn_id": TURN, "llm_tool_calls": [{"id": call, "type": "function", "function": {"name": "Read", "arguments": "{}"}}]}
        tm = {"turn_id": TURN, "tool_call_id": call, "assistant_step_id": step, "name": "Read"}
        rows.append((step, THREAD, "assistant", "step", json.dumps(sm), 100 + 2*i, TURN))
        rows.append((f"pressure-tool-{i}", THREAD, "tool", "x" * 1667, json.dumps(tm), 101 + 2*i, TURN))
    db.executemany("INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,active_turn_id) VALUES (?,?,?,?,?,'t0',?,?)", rows)
    db.commit()
    db.close()
    context = read_external_checkpoint_context(THREAD, MODEL, "codex", current_turn_id=TURN)
    assert context is not None and context.installed_checkpoint is not None
    async def reject_render(*_args):
        raise AssertionError("large recovery should use the estimate")
    result = await recovery_input_tokens(context, TurnIdentity(TURN, 0), 262_144, reject_render)
    assert result is not None and result >= 233_472
