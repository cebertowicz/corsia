"""Cold external checkpoints must not freeze outstanding live tool calls."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.checkpoint_types import CheckpointNotAdmitted
from core.turn_core import TurnIdentity
from llm.resolution import ExecutionTarget


@pytest.fixture
def debt_db(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        yield db
    finally:
        db.close()


def _context(db, monkeypatch, harness, *, outstanding):
    from infra import runtime_truth
    from llm import resolution
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    target = ExecutionTarget(
        catalog_id=f"{harness}/test", harness=harness,
        provider="openai" if harness == "codex" else "anthropic", backend="cli",
        transport_model="test", expected_reported_model=None, context_window=262_144,
        available=True, availability_reason=None, display_name="test",
        supports_reasoning=True, preserve_thinking_default=True,
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,next_message_seq,created_at,updated_at) "
        "VALUES ('debt','test',?,6,'t','t')", (target.catalog_id,),
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,turn_order,model,status,started_at,boot_epoch) "
        "VALUES ('current','debt',0,1,?,'running','t','boot')", (target.catalog_id,),
    )
    rows = [
        ("input", "user", "work", {"turn_id": "current"}, 1),
        ("older-step", "assistant", "older", {
            "turn_id": "current", "llm_tool_calls": [{
                "id": "older", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
        }, 2),
        ("later-step", "assistant", "later", {
            "turn_id": "current", "llm_tool_calls": [{
                "id": "later", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
        }, 4),
        ("later-result", "tool", "done", {
            "turn_id": "current", "assistant_step_id": "later-step",
            "tool_call_id": "later", "name": "bash",
        }, 5),
    ]
    if not outstanding:
        rows.append(("older-result", "tool", "done", {
            "turn_id": "current", "assistant_step_id": "older-step",
            "tool_call_id": "older", "name": "bash",
        }, 3))
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'debt',?,?,?,'t',?)",
        [(mid, role, content, json.dumps(meta), seq) for mid, role, content, meta, seq in rows],
    )
    db.commit()
    resolver = lambda *_args, **_kwargs: target
    monkeypatch.setattr(runtime_truth, "resolve_execution_target", resolver)
    monkeypatch.setattr(resolution, "resolve_execution_target", resolver)
    context = read_external_checkpoint_context(
        "debt", target.catalog_id, harness, current_turn_id="current",
    )
    assert context is not None and context.boundary is not None
    assert context.boundary.seq == (2 if outstanding else 3)
    return context


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.asyncio
async def test_cold_prepare_refuses_live_debt_before_publication(debt_db, monkeypatch, harness, required):
    from subagents.harness_checkpoint_resume import _prepare_external_checkpoint

    context = _context(debt_db, monkeypatch, harness, outstanding=True)
    live = SimpleNamespace(tool_step_ids={"older": "older-step"}, session=SimpleNamespace())
    with pytest.raises(CheckpointNotAdmitted, match="tool calls are outstanding"):
        await _prepare_external_checkpoint(
            "blocked", context=context, live=live, identity=TurnIdentity("current", 1),
            warm_source=None, required=required,
        )
    assert debt_db.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_settled_cold_admission_keeps_real_boundary(debt_db, monkeypatch, harness):
    from subagents.harness_checkpoint_cold_admission import build_external_cold_admission

    context = _context(debt_db, monkeypatch, harness, outstanding=False)
    admission = build_external_cold_admission(
        "settled", context=context,
        live=SimpleNamespace(tool_step_ids={}, session=SimpleNamespace()),
        identity=TurnIdentity("current", 1),
    )
    assert admission.request.mode == "cold"
    assert admission.request.through_seq == 3
    assert admission.provider_request.cold_source is not None


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.asyncio
async def test_warm_correction_rechecks_live_debt(debt_db, monkeypatch, harness):
    from subagents.harness_checkpoint_resume import _prepare_external_checkpoint

    context = _context(debt_db, monkeypatch, harness, outstanding=False)
    live = SimpleNamespace(tool_step_ids={}, session=SimpleNamespace())
    warm = await _prepare_external_checkpoint(
        "warm", context=context, live=live, identity=TurnIdentity("current", 1),
        warm_source=SimpleNamespace(harness=harness),
    )
    # Debt appearing after warm admission must still refuse the deferred cold correction.
    live.tool_step_ids["older"] = "older-step"
    with pytest.raises(CheckpointNotAdmitted, match="tool calls are outstanding"):
        await warm.provider_request.same_model_cold()
    assert debt_db.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
