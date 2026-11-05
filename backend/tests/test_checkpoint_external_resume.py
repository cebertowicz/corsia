from __future__ import annotations

import asyncio
import json
from random import Random
from string import ascii_letters, digits
from types import SimpleNamespace

import pytest

from core.turn_core import TurnIdentity
from external_harness_usage_helpers import persist_turn
from llm.resolution import ExecutionTarget

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login
DISCOVERED_MODELS = {'claude-code': ('claude-code/test',), 'codex': ('codex/test',)}


def _target(harness: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/test",
        harness=harness,
        provider="anthropic" if harness == "claude-code" else "openai",
        backend="cli",
        transport_model="test",
        expected_reported_model=None,
        context_window=262_144,
        available=True,
        availability_reason=None,
        display_name="test",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _cc_launch_context(snapshot_id: str):
    return SimpleNamespace(
        row={
            "spawned_by": None,
            "title": "cc",
            "model": "claude-code/test",
            "runtime_context_epoch": 3,
            "project_id": None,
        },
        is_subagent=False,
        model_alias="test",
        target_context_window=262_144,
        target_discovered_context_window=None,
        expected_reported_model=None,
        working_dir="/tmp",
        resume_session_id="provider-session",
        cc_session_id="provider-session",
        reconstruction=SimpleNamespace(
            snapshot_id=snapshot_id,
            snapshot=SimpleNamespace(to_dict=lambda: {}),
        ),
        tool_registry=None,
        tool_definitions=(),
        synthetic_resume=False,
        launch_identity=object(),
    )


def _seed_completed_turn_without_current_input(db, thread_id: str, model: str) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES (?, 'steve', ?, 0, 8, 't', 't')",
        (thread_id, model),
    )
    from connections.account_policy import require_claim
    from connections.vendor_accounts import VENDORS

    # Only a subscription vendor's Turn carries an account; a native one has none.
    provider = model.split("/", 1)[0]
    account_id = (require_claim(provider, thread_id=thread_id, model=model, db=db)
                  if provider in VENDORS else None)
    db.executemany(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,turn_order,model,status,started_at,ended_at,"
        "boot_epoch,vendor_account_id) VALUES (?,?,0,?,?,?,'t',?,'boot',?)",
        (
            ("turn-completed", thread_id, 1, model, "completed", "t2", account_id),
            ("turn-current", thread_id, 2, model, "running", None, account_id),
        ),
    )
    rows = (
        ("u1", "user", "start", {"turn_id": "turn-completed"}, 1),
        (
            "a2",
            "assistant",
            "first step",
            {
                "turn_id": "turn-completed",
                "llm_tool_calls": [
                    {"id": "call-1", "type": "function"},
                    {"id": "call-late", "type": "function"},
                ],
            },
            2,
        ),
        (
            "r3",
            "tool",
            "first result",
            {
                "turn_id": "turn-completed",
                "assistant_step_id": "a2",
                "tool_call_id": "call-1",
            },
            3,
        ),
        (
            "wake4",
            "system",
            "reviewer wake",
            {
                "turn_id": "turn-completed",
                "type": "context_event",
                "event_disposition": "wake",
            },
            4,
        ),
        (
            "r5",
            "tool",
            "late result",
            {
                "turn_id": "turn-completed",
                "assistant_step_id": "a2",
                "tool_call_id": "call-late",
            },
            5,
        ),
        (
            "a6",
            "assistant",
            "final step",
            {
                "turn_id": "turn-completed",
                "llm_tool_calls": [{"id": "call-2", "type": "function"}],
            },
            6,
        ),
        (
            "r7",
            "tool",
            "final result",
            {
                "turn_id": "turn-completed",
                "assistant_step_id": "a6",
                "tool_call_id": "call-2",
            },
            7,
        ),
    )
    db.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,'2026-09-01T00:00:00',?)",
        [
            (message_id, thread_id, role, content, json.dumps(metadata), seq)
            for message_id, role, content, metadata, seq in rows
        ],
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "continuity_snapshot_json,started_at,captured_at,installed_at,ended_at) "
        "VALUES ('floor',?,0,1,'u1','pressure','warm','installed',?,?,?,?,"
        "0,'[]','prior','[]','t','t','t','t')",
        (thread_id, model.split("/", 1)[0], model, model.split("/", 1)[0], model),
    )
    db.commit()


@pytest.mark.parametrize("harness", ("claude-code", "codex"))
def test_external_initial_context_uses_terminal_boundary_when_current_input_absent(
    isolated_db,
    monkeypatch,
    harness,
) -> None:
    from infra.database import get_db
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    target = _target(harness)
    thread_id = f"initial-hidden-{harness}"
    db = get_db()
    _seed_completed_turn_without_current_input(db, thread_id, target.catalog_id)
    db.close()
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda *_a, **_k: target,
    )
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir",
        lambda *_a, **_k: "/tmp",
    )

    context = read_external_checkpoint_context(
        thread_id,
        target.catalog_id,
        harness,
        current_turn_id="turn-current",
    )

    assert context is not None
    assert context.boundary == context.history.sealed_boundary
    assert context.boundary.seq == 7
    assert context.boundary.message_id == "r7"
    assert context.warm_boundary is not None
    assert context.warm_boundary.content == "final result"


def _running_turn_projection():
    from history.types import (
        HistoryBoundary,
        PortableHistoryProjection,
        PortableMessage,
        PortableTurn,
    )

    def row(message_id, seq, turn_id, role):
        return PortableMessage(message_id, seq, turn_id, role, message_id, "t")

    previous_boundary = HistoryBoundary("thread-1", 0, 2, "previous-answer")
    current_boundary = HistoryBoundary("thread-1", 0, 4, "current-answer")
    return PortableHistoryProjection(
        1, "thread-1", 0, None, current_boundary,
        (
            PortableTurn(
                "previous", "completed",
                (row("previous-input", 1, "previous", "user"), row("previous-answer", 2, "previous", "assistant")),
                previous_boundary,
            ),
            PortableTurn(
                "current", "running",
                (row("current-input", 3, "current", "user"), row("current-answer", 4, "current", "assistant")),
                current_boundary,
            ),
        ),
        True,
    ), previous_boundary


def test_external_boundary_uses_previous_turn_before_own_input() -> None:
    from subagents.harness_checkpoint_context import _external_boundary

    projection, previous = _running_turn_projection()

    assert _external_boundary(projection, None, current_turn_id="current") == previous


def test_external_boundary_keeps_own_input_at_the_floor() -> None:
    from subagents.harness_checkpoint_context import _external_boundary

    projection, previous = _running_turn_projection()

    assert _external_boundary(projection, previous, current_turn_id="current").seq == 3


def test_external_initial_context_does_not_fold_nonterminal_predecessor() -> None:
    from history.types import HistoryBoundary
    from subagents.harness_checkpoint_context import _external_boundary

    boundary = HistoryBoundary("thread-1", 0, 7, "last-row")
    projection = SimpleNamespace(
        turns=[SimpleNamespace(turn_id="turn-waiting", status="running")],
        sealed_boundary=boundary,
    )

    assert _external_boundary(
        projection,
        None,
        current_turn_id="turn-current",
    ) is None


@pytest.mark.parametrize("specimen", ("px", "governance"))
@pytest.mark.parametrize("trigger", ("pressure", "model_switch"))
def test_new_turn_cold_admission_fits_tool_run_after_installed_floor(
    isolated_db, monkeypatch, specimen: str, trigger: str,
) -> None:
    from core.fork_render import fit_cold_fork_input
    from infra.database import get_db
    from subagents.harness_checkpoint_cold_admission import build_external_cold_admission
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    target = _target("codex")
    thread_id = f"{specimen}-{trigger}-admission"
    floor_seq = 1810 if specimen == "px" else 11318
    counts = (297,) if specimen == "px" else (11,) * 4 + (10,) * 14 + (30,)
    body_size = 1_780 if specimen == "px" else 3_835
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'steve', ?, 't', 't')", (thread_id, target.catalog_id),
    )
    db.executemany(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,turn_order,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,?,0,?,?,?,'t',?,'boot')",
        (
            ("previous-turn", thread_id, 1, target.catalog_id, "error", "t"),
            ("new-turn", thread_id, 2, target.catalog_id, "running", None),
        ),
    )
    rows: list[tuple[str, str, str, str, dict[str, object], int]] = [
        ("floor", thread_id, "user", "prior input", {"turn_id": "previous-turn"}, floor_seq),
    ]
    next_seq = floor_seq + 1
    for group_index, count in enumerate(counts):
        step_id = f"step-{group_index}"
        calls = [{
            "id": f"call-{group_index}-{call_index}", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        } for call_index in range(count)]
        rows.append((step_id, thread_id, "assistant", "", {
            "turn_id": "previous-turn", "llm_tool_calls": calls,
        }, next_seq))
        next_seq += 1
        for call_index in range(count):
            call_id = f"call-{group_index}-{call_index}"
            generator = Random(group_index * 1_000 + call_index)
            result_size = body_size
            if specimen == "governance" and group_index == 18:
                result_size = 43_000 if call_index == 0 else 3_140
            body = f"{call_id}: " + "".join(
                generator.choices(ascii_letters + digits, k=result_size)
            )
            rows.append((f"result-{group_index}-{call_index}", thread_id, "tool", body, {
                "turn_id": "previous-turn", "tool_call_id": call_id,
                "assistant_step_id": step_id, "name": "read_file",
            }, next_seq))
            next_seq += 1
    last_result_seq = next_seq - 1
    rows.append(("new-input", thread_id, "user", "continue", {
        "turn_id": "new-turn",
    }, next_seq))
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,'t',?)",
        [(message_id, owner, role, body, json.dumps(meta), seq)
         for message_id, owner, role, body, meta, seq in rows],
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "continuity_snapshot_json,started_at,captured_at,installed_at,ended_at) "
        "VALUES ('prior',?,?,?,'floor','pressure',?,'installed',"
        "'codex',?,'codex',?,0,'[]','prior checkpoint','[]','t','t','t','t')",
        (thread_id, 0, floor_seq, "cold" if specimen == "px" else "warm",
         target.catalog_id, target.catalog_id),
    )
    db.commit()
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir",
        lambda *_args, **_kwargs: "/tmp",
    )
    context = read_external_checkpoint_context(
        thread_id, target.catalog_id, "codex", current_turn_id="new-turn",
    )
    assert context is not None
    assert context.boundary is not None
    assert context.boundary.seq == last_result_seq
    if trigger == "pressure":
        admission = build_external_cold_admission(
            f"{specimen}-{trigger}-recovery", context=context,
            live=SimpleNamespace(session=None), identity=SimpleNamespace(turn_id="new-turn"),
        )
    else:
        from infra.model_switch_types import SwitchRequest
        from thread.model_checkpoint import _target_cold_admission

        db.execute(
            "UPDATE threads SET pending_model=?, switch_operation_id='switch' WHERE id=?",
            (target.catalog_id, thread_id),
        )
        db.commit()
        admission = _target_cold_admission(
            SwitchRequest(
                "switch", thread_id, target.catalog_id, target.catalog_id,
                0, 1, 0, "user", "steve", "model_switch", "target", "t", 0,
            ),
            f"{specimen}-{trigger}-recovery",
        )
    assert admission.request.through_seq == last_result_seq
    assert admission.request.trigger == trigger
    assert admission.provider_request.cold_source is not None
    fitted = fit_cold_fork_input(
        admission.provider_request,
        # Fixed pressure budgets exercise the fit floor; these are synthetic
        # surface allowances, not measurements of production provider prompts.
        provider_surface_tokens=100_000 if specimen == "px" else 222_000,
    )
    text = "\n".join(block["content"] for block in fitted.messages)
    assert fitted.input_tokens < target.context_window
    assert fitted.first_included_seq is not None
    if specimen == "px":
        assert fitted.first_included_seq == floor_seq + 1
    assert "new-input" not in text
    assert f"call-{len(counts) - 1}-{counts[-1] - 1}" in text
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_checkpoint_exposes_only_boundary_source_inside_running_turn(
    isolated_db,
    monkeypatch,
    harness,
) -> None:
    from infra.database import get_db
    from subagents import harness_checkpoint_resume as bridge

    target = _target(harness)
    thread_id = f"running-{harness}"
    turn_id = f"turn-{harness}"
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'steve', ?, 't', 't')",
        (thread_id, target.catalog_id),
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?, ?, 0, ?, 'running', 't', 'boot')",
        (turn_id, thread_id, target.catalog_id),
    )
    rows = (
        ("u1", "user", "start", {"turn_id": turn_id}),
        ("a1", "assistant", "first", {
            "turn_id": turn_id,
            "llm_tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        }),
        ("r1", "tool", "first result", {
            "turn_id": turn_id, "tool_call_id": "call-1",
            "assistant_step_id": "a1", "name": "Read",
        }),
        ("a2", "assistant", "second", {
            "turn_id": turn_id,
            "llm_tool_calls": [{
                "id": "call-2", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        }),
        ("r2", "tool", "second result", {
            "turn_id": turn_id, "tool_call_id": "call-2",
            "assistant_step_id": "a2", "name": "Read",
        }),
    )
    db.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?, ?, ?, ?, ?, 't', ?)",
        [
            (message_id, thread_id, role, content, json.dumps(metadata), seq)
            for seq, (message_id, role, content, metadata) in enumerate(rows, 1)
        ],
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "continuity_snapshot_json,started_at,captured_at,installed_at,ended_at) "
        "VALUES ('previous',?,0,1,'u1','pressure','warm','installed',?,?,?,?,"
        "0,'[]','prior checkpoint','[]','t','t','t','t')",
        (
            thread_id,
            harness,
            target.catalog_id,
            harness,
            target.catalog_id,
        ),
    )
    db.commit()
    if harness == "claude-code":
        from registry.service import register_cc_session

        register_cc_session(
            thread_id, "provider-continuation",
            checkpoint_generation_id="previous", db=db,
        )
    else:
        from registry.service import register_codex_thread

        register_codex_thread(
            thread_id, "provider-continuation",
            checkpoint_generation_id="previous", db=db,
        )
    db.commit()
    db.close()

    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir",
        lambda *_args, **_kwargs: "/tmp",
    )

    context = bridge.read_external_checkpoint_context(
        thread_id, target.catalog_id, harness,
    )

    assert context is not None
    assert context.boundary is not None
    assert context.boundary.seq == 3
    assert context.boundary.message_id == "r1"
    assert context.active_checkpoint is None
    assert context.installed_checkpoint.id == "previous"
    assert context.registered_checkpoint_id == "previous"
    assert context.eager_checkpoint_id == "previous"
    assert context.warm_boundary is not None
    assert context.warm_boundary.role == "tool"
    assert context.warm_boundary.content == "first result"
    assert context.warm_boundary.tool_call_id == "call-1"

    admission = await bridge._prepare_external_checkpoint(
        "checkpoint-1",
        context=context,
        live=SimpleNamespace(session=SimpleNamespace(mcp_config=None)),
        identity=TurnIdentity(turn_id, 1),
        warm_source=None,
    )

    assert admission is not None
    assert admission.request.mode == "cold"
    frozen = admission.provider_request.cold_source
    assert frozen is not None
    source = "\n".join(str(message.content) for message in frozen.projection.messages)
    assert "first result" in source
    assert "second" not in source


@pytest.mark.asyncio
async def test_external_initial_checkpoint_warm_source_matches_completed_boundary(
    isolated_db,
    monkeypatch,
) -> None:
    from infra.database import get_db
    from subagents import harness_checkpoint_resume as bridge

    target = _target("codex")
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('warm-initial','steve',?,'t','t')",
        (target.catalog_id,),
    )
    db.executemany(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,'warm-initial',0,?,?, 't',?,'boot')",
        (
            ("completed-turn", target.catalog_id, "completed", "t"),
            ("running-turn", target.catalog_id, "running", None),
        ),
    )
    db.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'warm-initial',?,?,?,'t',?)",
        (
            ("u1", "user", "first ask", json.dumps({"turn_id": "completed-turn"}), 1),
            ("a1", "assistant", "tool call", json.dumps({
                "turn_id": "completed-turn",
                "llm_tool_calls": [{"id": "exec-previous", "type": "function"}],
            }), 2),
            ("r1", "tool", "tool result", json.dumps({
                "turn_id": "completed-turn",
                "tool_call_id": "exec-previous",
                "assistant_step_id": "a1",
            }), 3),
            ("a2", "assistant", "first answer", json.dumps({"turn_id": "completed-turn"}), 4),
            ("u2", "user", "next ask", json.dumps({"turn_id": "running-turn"}), 5),
        ),
    )
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "continuity_snapshot_json,started_at,captured_at,installed_at,ended_at) "
        "VALUES ('warm-floor','warm-initial',0,1,'u1','pressure','warm',"
        "'installed','codex',?,'codex',?,0,'[]','prior','[]','t','t','t','t')",
        (target.catalog_id, target.catalog_id),
    )
    db.commit()
    db.close()
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda *_a, **_k: target,
    )
    monkeypatch.setattr(
        "subagents.codex_runtime_support.resolve_working_dir",
        lambda *_a, **_k: "/tmp",
    )
    context = bridge.read_external_checkpoint_context(
        "warm-initial", target.catalog_id, "codex",
    )
    assert context is not None
    assert context.boundary is not None
    assert context.boundary.seq == 4
    assert context.warm_boundary is not None
    assert context.warm_boundary.role == "assistant"
    assert context.warm_boundary.content == "first answer"
    live = SimpleNamespace(session=SimpleNamespace())
    admission = await bridge._prepare_external_checkpoint(
        "checkpoint-1",
        context=context,
        live=live,
        identity=TurnIdentity("running-turn", 2),
        warm_source=object(),
    )

    assert admission is not None
    assert admission.request.through_seq == 4
    assert admission.request.mode == "warm"

    fallback = await bridge._prepare_external_checkpoint(
        "checkpoint-2",
        context=context,
        live=SimpleNamespace(session=SimpleNamespace()),
        identity=TurnIdentity("running-turn", 2),
        warm_source=None,
    )

    assert fallback is not None
    assert fallback.request.mode == "cold"
    assert fallback.request.through_seq == 4
    frozen = fallback.provider_request.cold_source
    assert frozen is not None
    source = "\n".join(str(message.content) for message in frozen.projection.messages)
    assert "first answer" in source
    assert "next ask" not in source

    # Codex compacts at its own hard wall, so an optional no-warm fold must
    # cold-admit before the provider reaches it.
    optional_fallback = await bridge._prepare_external_checkpoint(
        "checkpoint-3",
        context=context,
        live=SimpleNamespace(session=SimpleNamespace()),
        identity=TurnIdentity("running-turn", 2),
        warm_source=None,
        required=False,
    )

    assert optional_fallback is not None
    assert optional_fallback.request.mode == "cold"
    assert optional_fallback.request.through_seq == 4

    from core.checkpoint_types import OptionalCheckpointSkipped
    from dataclasses import replace

    with pytest.raises(
        OptionalCheckpointSkipped,
        match="no warm source for an optional fold",
    ):
        await bridge._prepare_external_checkpoint(
            "checkpoint-cc",
            context=replace(context, target=_target("claude-code")),
            live=SimpleNamespace(session=SimpleNamespace()),
            identity=TurnIdentity("running-turn", 2),
            warm_source=None,
            required=False,
        )


@pytest.mark.asyncio
async def test_claude_initial_resume_captures_exact_boundary_warm_source(monkeypatch):
    """Claude provider-resume must not silently fall through to cold capture."""
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge
    from core import fork_cc_boundary

    target = _target("claude-code")
    boundary = HistoryBoundary("thread-cc", 0, 7, "through-row")
    context = bridge.ExternalCheckpointContext(
        thread_row={}, target=target, history=object(), boundary=boundary,
        previous_generation_id="previous", working_dir="/tmp",
        tail_tokens=100, minimum_tail_tokens=10,
        warm_boundary=SimpleNamespace(message_id="through-row"),
    )
    warm_source = object()
    captured = {}

    async def capture(received_context, live, thread_id):
        assert received_context is context
        assert thread_id == "thread-cc"
        return warm_source

    async def prepare(_checkpoint_id, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(request=SimpleNamespace(mode="warm"))

    monkeypatch.setattr(
        fork_cc_boundary,
        "capture_boundary_warm_source",
        capture,
        raising=False,
    )
    monkeypatch.setattr(bridge, "_prepare_external_checkpoint", prepare)

    admission = await bridge._prepare_initial_external_checkpoint(
        "checkpoint-cc", True,
        context=context,
        live=object(),
        thread_id="thread-cc",
        identity=TurnIdentity("turn-cc", 1),
        prepared=SimpleNamespace(mode="provider_resume"),
    )

    assert admission.request.mode == "warm"
    assert captured["warm_source"] is warm_source


@pytest.mark.asyncio
async def test_provider_resume_refusal_keeps_its_own_reason(monkeypatch) -> None:
    from core import fork_cc_boundary
    from core.fork_capture import warm_capture_refused
    from history.types import HistoryBoundary
    from loguru import logger
    from subagents import harness_checkpoint_resume as bridge

    context = bridge.ExternalCheckpointContext(
        thread_row={}, target=_target("claude-code"), history=object(),
        boundary=HistoryBoundary("thread-cc", 0, 7, "through-row"),
        previous_generation_id="previous", working_dir="/tmp", tail_tokens=100,
        minimum_tail_tokens=10, warm_boundary=SimpleNamespace(message_id="through-row"),
    )

    async def capture(_context, _live, thread_id):
        return warm_capture_refused(thread_id, "claude-code", "live_changed")

    async def prepare(_checkpoint_id, **kwargs):
        return SimpleNamespace(request=SimpleNamespace(mode="cold"), **kwargs)

    monkeypatch.setattr(fork_cc_boundary, "capture_boundary_warm_source", capture)
    monkeypatch.setattr(bridge, "_prepare_external_checkpoint", prepare)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="DEBUG")
    try:
        admission = await bridge._prepare_initial_external_checkpoint(
            "checkpoint-cc", True, context=context, live=object(), thread_id="thread-cc",
            identity=TurnIdentity("turn-cc", 1), prepared=SimpleNamespace(mode="provider_resume"),
        )
    finally:
        logger.remove(sink)

    assert admission.warm_source is None
    refused = "fork warm capture refused for thread-cc (claude-code): live_changed"
    assert lines.count(f"INFO|{refused}\n") == 1
    assert not any("no_live_handle" in line for line in lines)


@pytest.mark.asyncio
async def test_codex_rollout_refusal_keeps_its_own_reason(monkeypatch) -> None:
    from core import fork_render
    from core.fork_capture import warm_capture_refused
    from history.types import HistoryBoundary
    from loguru import logger
    from subagents import harness_checkpoint_resume as bridge

    context = bridge.ExternalCheckpointContext(
        thread_row={}, target=_target("codex"), history=object(),
        boundary=HistoryBoundary("thread-codex", 0, 7, "through-row"),
        previous_generation_id="previous", working_dir="/tmp", tail_tokens=100,
        minimum_tail_tokens=10, binary="codex", reasoning_effort="low",
    )

    async def capture(thread_id, *_args, **_kwargs):
        return warm_capture_refused(thread_id, "codex", "no_warm_boundary")

    async def prepare(_checkpoint_id, **kwargs):
        return SimpleNamespace(request=SimpleNamespace(mode="cold"), **kwargs)

    monkeypatch.setattr(fork_render, "capture_warm_fork_source", capture)
    monkeypatch.setattr(bridge, "_prepare_external_checkpoint", prepare)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="DEBUG")
    try:
        admission = await bridge._prepare_initial_external_checkpoint(
            "checkpoint-codex", True, context=context, live=object(), thread_id="thread-codex",
            identity=TurnIdentity("turn-codex", 1), prepared=SimpleNamespace(mode="exact_replay"),
        )
    finally:
        logger.remove(sink)

    assert admission.warm_source is None
    refused = "fork warm capture refused for thread-codex (codex): no_warm_boundary"
    assert lines.count(f"INFO|{refused}\n") == 1
    assert not any("no_live_handle" in line for line in lines)


@pytest.mark.asyncio
async def test_codex_fresh_process_captures_registered_rollout(monkeypatch) -> None:
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    boundary = HistoryBoundary("thread-codex", 0, 7, "through-row")
    context = bridge.ExternalCheckpointContext(
        thread_row={}, target=_target("codex"), history=object(), boundary=boundary,
        previous_generation_id="previous", working_dir="/tmp", tail_tokens=100,
        minimum_tail_tokens=10, binary="codex", reasoning_effort="low",
    )
    live = object()
    warm_source = object()
    captured = {}

    async def capture(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return warm_source

    async def prepare(_checkpoint_id, **kwargs):
        captured["warm_source"] = kwargs["warm_source"]
        return SimpleNamespace(request=SimpleNamespace(mode="warm"))

    monkeypatch.setattr("core.fork_render.capture_warm_fork_source", capture)
    monkeypatch.setattr(bridge, "_prepare_external_checkpoint", prepare)

    admission = await bridge._prepare_initial_external_checkpoint(
        "checkpoint-codex", True, context=context, live=live,
        thread_id="thread-codex", identity=TurnIdentity("turn-codex", 1),
        prepared=SimpleNamespace(mode="exact_replay"),
    )

    assert admission.request.mode == "warm"
    assert captured["warm_source"] is warm_source
    assert captured["args"] == ("thread-codex", boundary)
    assert captured["kwargs"] == {
        "target": context.target, "binary": "codex", "reasoning_effort": "low",
        "live": live,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("mode", ["exact_replay", "checkpoint_tail"])
@pytest.mark.parametrize("required", [False, True])
async def test_fresh_process_initial_fold_follows_harness_no_warm_policy(
    monkeypatch, harness, mode, required,
):
    """A fresh external process follows its harness's no-warm policy.

    Claude Code has automatic compaction disabled, so an optional no-warm fold
    waits for a later warm boundary. Codex compacts at its hard wall and cold-admits.
    """
    from core.checkpoint_resume import CheckpointWaitScope, ProviderBoundary, _start_checkpoint
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    boundary = HistoryBoundary("thread-fresh", 0, 7, "through-row")
    context = bridge.ExternalCheckpointContext(
        thread_row={}, target=_target(harness), history=object(), boundary=boundary,
        previous_generation_id="previous", working_dir="/tmp",
        tail_tokens=100, minimum_tail_tokens=10,
        warm_boundary=SimpleNamespace(message_id="through-row"),
    )
    cold_calls = []

    def cold(_checkpoint_id, **kwargs):
        cold_calls.append(kwargs)
        return SimpleNamespace(request=SimpleNamespace(mode="cold"))

    monkeypatch.setattr(bridge, "build_external_cold_admission", cold)

    async def prepare_initial(checkpoint_id, required):
        return await bridge._prepare_initial_external_checkpoint(
            checkpoint_id, required,
            context=context,
            live=object(),
            thread_id="thread-fresh",
            identity=TurnIdentity("turn-fresh", 1),
            prepared=SimpleNamespace(mode=mode),
        )

    from loguru import logger

    async def render_installed(_record):
        raise AssertionError("optional skip must not render an installed checkpoint")

    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="DEBUG")
    try:
        if harness == "claude-code" and not required:
            admission = await _start_checkpoint(
                ProviderBoundary(
                    thread_id="thread-fresh",
                    turn=TurnIdentity("turn-fresh", 1),
                    context_window=100_000,
                    canonical_input_tokens=None,
                    provider_input_tokens=None,
                    minimum_input_tokens=0,
                    adopted_checkpoint_id=None,
                    active_checkpoint=None,
                    installed_checkpoint=None,
                    prepare_checkpoint=prepare_initial,
                    render_installed=render_installed,
                    wait_scope=CheckpointWaitScope(),
                ),
                required=False,
            )
        else:
            admission = await prepare_initial("checkpoint-fresh", required)
    finally:
        logger.remove(sink)

    if harness == "claude-code" and not required:
        assert admission is None
        assert cold_calls == []
        assert sum(line.startswith("INFO|Optional checkpoint skipped") for line in lines) == 1
        refused = (
            "INFO|fork warm capture refused for thread-fresh "
            f"(claude-code): no_live_handle mode={mode} has_session=no "
            "session_registered=no eager=none\n"
        )
        assert lines.count(refused) == 1
        assert not any(line.startswith("WARNING|") for line in lines)
    else:
        assert admission.request.mode == "cold"
        assert [call["context"] for call in cold_calls] == [context]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {},
            "no_live_handle mode=exact_replay has_session=no session_registered=no eager=none",
        ),
        (
            {"provider_registered": True},
            "no_live_handle mode=exact_replay has_session=yes session_registered=yes eager=none",
        ),
        (
            {"provider_registered": True, "eager_checkpoint_id": "eager-1"},
            "no_live_handle mode=exact_replay has_session=no session_registered=yes "
            "eager=pending",
        ),
        (
            {
                "provider_registered": True,
                "eager_checkpoint_id": "eager-1",
                "registered_checkpoint_id": "eager-1",
            },
            "no_live_handle mode=exact_replay has_session=yes session_registered=yes "
            "eager=adopted",
        ),
    ],
)
def test_no_live_handle_names_why_the_launch_was_not_provider_resume(fields, expected):
    from core.checkpoint_warm_request import _no_live_handle_reason
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    context = bridge.ExternalCheckpointContext(
        thread_row={}, target=_target("claude-code"), history=object(),
        boundary=HistoryBoundary("thread-fresh", 0, 7, "through-row"),
        previous_generation_id=None, working_dir="/tmp",
        tail_tokens=100, minimum_tail_tokens=10, **fields,
    )

    assert _no_live_handle_reason(SimpleNamespace(mode="exact_replay"), context) == expected


@pytest.mark.asyncio
async def test_codex_tool_boundary_prepares_warm_checkpoint_at_previous_group(
    monkeypatch,
) -> None:
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge
    from subagents.codex_runtime_state import _LIVE

    thread_id = "thread-codex-warm"
    identity = TurnIdentity("turn-codex-warm", 4)
    target = _target("codex")
    boundary = HistoryBoundary(thread_id, 0, 3, "previous-result")
    context = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=boundary,
        previous_generation_id="checkpoint-previous",
        working_dir="/tmp",
        tail_tokens=12_000,
        minimum_tail_tokens=1_000,
        warm_boundary=SimpleNamespace(
            role="tool",
            content="previous result",
            tool_call_id="exec-previous",
        ),
    )
    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        context_window=100_000,
        session=SimpleNamespace(),
        tool_step_ids=set(),
    )
    _LIVE.put(thread_id, live)
    captured = {}
    resumed = []

    monkeypatch.setattr(
        bridge, "read_external_checkpoint_context", lambda *_a, **_k: context,
    )
    monkeypatch.setattr(bridge, "external_measurement", lambda *_a: (90_000, 100_000))
    scan_lock_states = []

    def boundary_prefix(received_thread, rollout_boundary):
        scan_lock_states.append(_LIVE.lock(thread_id).locked())
        return (
            321
            if (
                received_thread == thread_id
                and rollout_boundary.tool_call_id == "exec-previous"
            )
            else None
        )

    monkeypatch.setattr(
        "core.fork_codex.warm_tool_boundary_prefix_bytes",
        boundary_prefix,
    )

    async def capture(received_thread, received_boundary, **kwargs):
        async with _LIVE.lock(thread_id):
            assert _LIVE.get(thread_id) is live
        assert received_thread == thread_id
        assert received_boundary == boundary
        assert kwargs["prefix_bytes"] == 321
        return object()

    async def maintain(provider_boundary, **options):
        admission = await provider_boundary.prepare_checkpoint(
            "checkpoint-warm",
            required=True,
        )
        captured["admission"] = admission
        captured["options"] = options
        return ProviderContinuation(
            BoundaryDecision.ADOPT_AND_PROCEED,
            identity,
            payload=admission,
            checkpoint_id="checkpoint-warm",
        )

    async def resume(*args):
        resumed.append(args)

    monkeypatch.setattr(
        "core.fork_render.capture_warm_fork_source",
        capture,
    )
    monkeypatch.setattr(
        "core.checkpoint_resume.maintain_before_provider_call",
        maintain,
    )
    monkeypatch.setattr(bridge, "_resume_external", resume)

    try:
        assert await bridge.maintain_external_tool_boundary(
            live,
            thread_id,
            "codex",
        ) is True
        assert scan_lock_states == [False]
    finally:
        _LIVE.pop(thread_id, None)

    admission = captured["admission"]
    assert admission is not None
    assert admission.request.mode == "warm"
    assert admission.request.through_seq == boundary.seq
    assert admission.request.through_message_id == boundary.message_id
    assert captured["options"] == {"must_adopt": False}
    assert resumed == [(live, thread_id, admission, "codex")]


@pytest.fixture
def stub_recovery_measurement(monkeypatch):
    # The real recovery path is covered in test_codex_canonical_pressure.py.
    async def no_pressure(*_args):
        return None
    monkeypatch.setattr("subagents.harness_checkpoint_resume.recovery_input_tokens", no_pressure)


@pytest.mark.usefixtures("stub_recovery_measurement")
@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
@pytest.mark.parametrize("fresh_state", ("boundary", "winner", "no_boundary"))
async def test_external_tool_boundary_uses_fresh_checkpoint_state(
    monkeypatch,
    harness,
    fresh_state,
) -> None:
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    target = _target(harness)
    identity = TurnIdentity(f"turn-{harness}", 2)
    winner = SimpleNamespace(id="checkpoint-installed", adopted_input_tokens=None)
    first = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=HistoryBoundary("thread-1", 0, 3, "old-boundary"),
        previous_generation_id="checkpoint-old",
        working_dir="/tmp",
        tail_tokens=1_000,
        minimum_tail_tokens=500,
    )
    fresh = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=(
            HistoryBoundary("thread-1", 0, 5, "fresh-boundary")
            if fresh_state == "boundary"
            else None
        ),
        previous_generation_id=(
            winner.id if fresh_state == "winner" else "checkpoint-old"
        ),
        working_dir="/tmp",
        tail_tokens=500,
        minimum_tail_tokens=250,
        installed_checkpoint=winner if fresh_state == "winner" else None,
        eager_checkpoint_id=winner.id if fresh_state == "winner" else None,
    )
    contexts = iter(
        (first, fresh, fresh) if fresh_state == "boundary" else (first, fresh)
    )
    reads = []

    def read_context(*args, **kwargs):
        reads.append((args, kwargs))
        return next(contexts)

    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        context_window=100_000,
        session=SimpleNamespace(),
    )
    warm_source = object()
    captured = {}
    monkeypatch.setattr(bridge, "read_external_checkpoint_context", read_context)
    monkeypatch.setattr(bridge, "external_measurement", lambda *_a: (95_000, 100_000))

    async def capture(context, *_args):
        captured["source_context"] = context
        return warm_source

    async def prepare(_checkpoint_id, **kwargs):
        captured["prepare_context"] = kwargs["context"]
        captured["warm_source"] = kwargs["warm_source"]
        return object()

    async def maintain(boundary, **_kwargs):
        captured["installed"] = boundary.installed_checkpoint
        captured["prepare_available"] = boundary.prepare_checkpoint is not None
        if boundary.prepare_checkpoint is not None:
            await boundary.prepare_checkpoint("checkpoint-next", True)
        return ProviderContinuation(BoundaryDecision.PROCEED, identity)

    monkeypatch.setattr(bridge, "_capture_external_tool_boundary_warm_source", capture)
    monkeypatch.setattr(bridge, "_prepare_external_checkpoint", prepare)
    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)

    assert await bridge.maintain_external_tool_boundary(
        live, "thread-1", harness,
    ) is False
    assert len(reads) == (3 if fresh_state == "boundary" else 2)
    expected = {
        "installed": winner if fresh_state == "winner" else None,
        "prepare_available": fresh_state == "boundary",
    }
    if fresh_state == "boundary":
        expected.update({
            "source_context": fresh,
            "prepare_context": fresh,
            "warm_source": warm_source,
        })
    assert captured == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_tool_boundary_adopts_winner_seen_by_fresh_read(
    stub_recovery_measurement, monkeypatch,
    harness,
) -> None:
    from core.checkpoint_resume import CheckpointWaitScope
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    target = _target(harness)
    identity = TurnIdentity(f"turn-{harness}", 2)
    winner = SimpleNamespace(id="checkpoint-installed", adopted_input_tokens=None)
    first = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=HistoryBoundary("thread-1", 0, 3, "old-boundary"),
        previous_generation_id="checkpoint-old",
        working_dir="/tmp",
        tail_tokens=1_000,
        minimum_tail_tokens=500,
    )
    fresh = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=None,
        previous_generation_id=winner.id,
        working_dir="/tmp",
        tail_tokens=500,
        minimum_tail_tokens=250,
        installed_checkpoint=winner,
        eager_checkpoint_id=winner.id,
    )
    contexts = iter((first, fresh))
    resumed = []

    monkeypatch.setattr(
        bridge,
        "read_external_checkpoint_context",
        lambda *_args, **_kwargs: next(contexts),
    )
    monkeypatch.setattr(bridge, "external_measurement", lambda *_a: (95_000, 100_000))
    monkeypatch.setattr(
        "core.checkpoint_resume.installed_replay",
        lambda record: SimpleNamespace(checkpoint_generation_id=record.id),
    )

    async def render(_context, _identity, selection, _input_identity):
        assert selection.checkpoint_generation_id == winner.id
        return SimpleNamespace(rendered=SimpleNamespace(input_tokens=12_000))

    async def reject_prepare(*_args, **_kwargs):
        raise AssertionError("an installed winner must bypass checkpoint rendering")

    async def resume(live, thread_id, prepared, received_harness):
        resumed.append((live, thread_id, prepared, received_harness))

    monkeypatch.setattr(bridge, "render_external_selection", render)
    monkeypatch.setattr(bridge, "_prepare_external_checkpoint", reject_prepare)
    monkeypatch.setattr(bridge, "_resume_external", resume)

    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        context_window=100_000,
        session=SimpleNamespace(),
    )
    assert await bridge.maintain_external_tool_boundary(
        live, "thread-1", harness,
    ) is True
    assert len(resumed) == 1
    assert resumed[0][0:2] == (live, "thread-1")
    assert resumed[0][3] == harness
    assert resumed[0][2].rendered.input_tokens == 12_000


@pytest.mark.asyncio
async def test_cc_checkpoint_replaces_handle_beneath_same_turn(
    isolated_db,
    monkeypatch,
) -> None:
    from subagents import cc_runtime
    from subagents import cc_resume_transition as resume_module
    from subagents.cc_runtime_state import _LIVE, _Live
    from core.fork_cc_context import CCForkContext

    identity = TurnIdentity("turn-cc", 4)
    terminated = []

    def session(name):
        async def terminate() -> None:
            terminated.append(name)

        return SimpleNamespace(
            binary="claude",
            effort="high",
            claude_dir="/tmp/claude",
            working_dir="/tmp/project",
            terminate=terminate,
            tool_context=CCForkContext(None, None),
        )

    old_session = session("old")
    old = _Live(
        session=old_session,
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        turn_active=True,
        catalog_model_id="claude-code/test",
    )
    replacements = [
        _Live(session=session("replacement-1"), parent_thread_id=None, name="cc"),
        _Live(session=session("replacement-2"), parent_thread_id=None, name="cc"),
    ]
    _LIVE.put("thread-cc", old)
    wait_scope = old.checkpoint_wait_scope

    async def resume_child(*_args, **_kwargs):
        current = _LIVE.get("thread-cc")
        assert current is not None
        assert current.corsia_turn == identity
        assert _kwargs["expected_live"] is current
        assert _kwargs["continuing_identity"] == identity
        assert _kwargs["continuing_wait_scope"] is wait_scope
        replacement = replacements.pop(0)
        replacement.corsia_turn = _kwargs["continuing_identity"]
        replacement.checkpoint_wait_scope = _kwargs["continuing_wait_scope"]
        _LIVE.put("thread-cc", replacement)
        return replacement

    accepted = []

    async def accept(_thread_id, live, _frame, **kwargs):
        accepted.append(dict(live=live, **kwargs))
        live.corsia_turn = kwargs["preadmitted_turn"]
        live.turn_active = True
        return kwargs["preadmitted_turn"]

    monkeypatch.setattr(resume_module, "resume_child", resume_child)
    monkeypatch.setattr(resume_module, "build_cc_user_frame", lambda **_kwargs: object())
    monkeypatch.setattr(resume_module, "accept_reconstructed_input", accept)
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: _target("claude-code"),
    )

    def prepared(generation_id: str, epoch: int):
        return SimpleNamespace(
            runtime_context_epoch=epoch,
            replay=SimpleNamespace(
                    checkpoint_generation_id=generation_id,
                    tail=SimpleNamespace(resume_presence_line=None),
                ),
            fit_ledger=SimpleNamespace(rendered_input_tokens=12000 + epoch),
        )

    first = await cc_runtime.resume_same_turn_after_checkpoint(
        "thread-cc",
        old,
        prepared("checkpoint-1", 3),
    )
    first_replacement = _LIVE.get("thread-cc")
    result = await cc_runtime.resume_same_turn_after_checkpoint(
        "thread-cc",
        first_replacement,
        prepared("checkpoint-2", 4),
    )

    assert first == identity
    assert result == identity
    assert terminated == ["old", "replacement-1"]
    assert _LIVE.get("thread-cc").session is not first_replacement.session
    assert _LIVE.get("thread-cc").session.tool_context.tokens == 12004
    assert _LIVE.get("thread-cc").checkpoint_wait_scope is wait_scope
    assert [entry["preadmitted_turn"] for entry in accepted] == [identity, identity]
    assert [entry["turn_id"] for entry in accepted] == [
        identity.turn_id,
        identity.turn_id,
    ]


@pytest.mark.asyncio
async def test_cc_mid_turn_replacement_observes_stop_before_provider_write(
    monkeypatch,
) -> None:
    from subagents import cc_runtime
    from subagents import cc_resume_transition as resume_module
    from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn

    thread_id = "thread-cc-stop-after-replacement"
    identity = TurnIdentity("turn-cc-stop-after-replacement", 8)
    persist_turn(thread_id, identity.turn_id)
    provider_writes: list[str] = []

    class ReplacementSession:
        async def feed(self, *_args, **_kwargs) -> None:
            provider_writes.append("feed")
            raise AssertionError("Stop-latched replacement reached provider write")

    replacement = _Live(
        session=ReplacementSession(), parent_thread_id=None, name="replacement",
    )

    class OldSession:
        binary = "claude"
        effort = "high"
        claude_dir = "/tmp/claude"

        async def terminate(self) -> None:
            replacement.checkpoint_wait_scope.request_stop()

    old = _Live(
        session=OldSession(),
        parent_thread_id=None,
        name="old",
        corsia_turn=identity,
        turn_active=True,
        catalog_model_id="claude-code/test",
    )
    wait_scope = old.checkpoint_wait_scope
    replacement.corsia_turn = identity
    replacement.checkpoint_wait_scope = wait_scope
    assert activate_bound_turn(replacement, identity)

    async def resume_child(*_args, **_kwargs):
        assert _kwargs["continuing_identity"] == identity
        assert _kwargs["continuing_wait_scope"] is wait_scope
        _LIVE.put(thread_id, replacement)
        return replacement

    monkeypatch.setattr(resume_module, "resume_child", resume_child)
    monkeypatch.setattr(
        resume_module,
        "build_cc_user_frame",
        lambda **_kwargs: SimpleNamespace(
            snapshot_id="snapshot-stop-after-replacement",
            snapshot=SimpleNamespace(to_dict=lambda: {}),
            turn_text="mid-turn anchor",
        ),
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: _target("claude-code"),
    )
    _LIVE.reset()
    _LIVE.put(thread_id, old)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cc_runtime.resume_same_turn_after_checkpoint(
                thread_id,
                old,
                SimpleNamespace(
                    runtime_context_epoch=9,
                    replay=SimpleNamespace(tail=SimpleNamespace(resume_presence_line=None)),
                    fit_ledger=SimpleNamespace(rendered_input_tokens=12000),
                ),
            )
        assert _LIVE.get(thread_id) is replacement
        assert replacement.corsia_turn == identity
        assert replacement.checkpoint_wait_scope is wait_scope
        assert replacement.checkpoint_wait_scope.stop_requested is True
        assert provider_writes == []
    finally:
        _LIVE.reset()


@pytest.mark.asyncio
async def test_cc_checkpoint_replacement_is_bound_before_start_yields(
    monkeypatch,
) -> None:
    from subagents import cc_runtime_helpers, cc_startup_process
    from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn

    thread_id = "thread-cc-bound-launch"
    identity = TurnIdentity("turn-cc-bound-launch", 7)
    old = _Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="old",
        corsia_turn=identity,
    )
    assert activate_bound_turn(old, identity)
    wait_scope = old.checkpoint_wait_scope
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    launched: list[_Live] = []

    class Session:
        async def start(self) -> None:
            assert len(launched) == 1
            replacement = launched[0]
            assert _LIVE.get(thread_id) is replacement
            assert replacement.corsia_turn == identity
            assert replacement.checkpoint_wait_scope is wait_scope
            assert replacement.turn_active is True
            start_entered.set()
            await release_start.wait()

    context = _cc_launch_context("snapshot-bound-launch")

    def build_session(_thread_id, live, *_args, **_kwargs):
        assert _LIVE.get(thread_id) is old
        assert old.corsia_turn == identity
        assert old.turn_active is True
        assert live is not old
        assert live.corsia_turn == identity
        assert live.checkpoint_wait_scope is wait_scope
        launched.append(live)
        return Session()

    monkeypatch.setattr(cc_startup_process, "resolve_claude_dir", lambda _: "/tmp")
    monkeypatch.setattr(cc_startup_process, "build_starting_session", build_session)
    monkeypatch.setattr(
        cc_startup_process, "install_starting_identity", lambda *_args: None,
    )
    monkeypatch.setattr(cc_runtime_helpers, "_validate_working_dir", lambda *_: None)
    _LIVE.reset()
    _LIVE.put(thread_id, old)
    lock = _LIVE.lock(thread_id)
    await lock.acquire()
    task = asyncio.create_task(cc_startup_process.launch_cc_process(
        thread_id,
        context,
        binary="claude",
        claude_dir=None,
        effort=None,
        broadcast=lambda *_args: None,
        seed_file="",
        source_artifacts=[],
        surface_exit_error_fn=lambda *_args: None,
        finalize_fn=lambda *_args, **_kwargs: None,
        db=object(),
        held_lock=lock,
        expected_live=old,
        continuing_identity=identity,
        continuing_wait_scope=wait_scope,
    ))
    try:
        start_waiter = asyncio.create_task(start_entered.wait())
        done, _ = await asyncio.wait(
            (task, start_waiter),
            timeout=0.5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done
        if task in done:
            task.result()
        await asyncio.wait_for(start_waiter, timeout=0.5)
        assert len(launched) == 1
        assert _LIVE.get(thread_id) is launched[0]
        assert launched[0].corsia_turn == identity
        assert launched[0].checkpoint_wait_scope is wait_scope
        assert launched[0].turn_active is True
        assert isinstance(launched[0].turn_terminal, asyncio.Event)
        release_start.set()
        assert await asyncio.wait_for(task, timeout=0.5) is launched[0]
    finally:
        release_start.set()
        await asyncio.gather(task, return_exceptions=True)
        lock.release()
        _LIVE.reset()


@pytest.mark.asyncio
async def test_cc_checkpoint_replacement_rejects_retired_predecessor_before_start(
    isolated_db,
    monkeypatch,
) -> None:
    from infra.database import get_db
    from subagents import (
        cc_runtime,
        cc_runtime_helpers,
        cc_runtime_resume,
        cc_startup_process,
        cc_stop,
    )
    from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn

    thread_id = "thread-cc-retired-before-replacement-publish"
    identity = TurnIdentity("turn-cc-retired-before-replacement-publish", 7)

    old = _Live(
        session=SimpleNamespace(
            binary="claude", effort="high", claude_dir="/tmp/claude",
        ),
        parent_thread_id=None,
        name="old",
        corsia_turn=identity,
        catalog_model_id="claude-code/test",
    )
    assert activate_bound_turn(old, identity)
    wait_scope = old.checkpoint_wait_scope
    launch_paused = asyncio.Event()
    release_launch = asyncio.Event()
    starts: list[bool] = []
    identity_installs: list[bool] = []

    class Session:
        async def start(self) -> None:
            starts.append(True)

    context = _cc_launch_context("snapshot-retired-before-replacement-publish")

    def install_identity(*_args, **_kwargs):
        identity_installs.append(True)

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?, 'user-1', 'claude-code/test', 't0', 't0')",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: _target("claude-code"),
    )
    monkeypatch.setattr(
        cc_runtime_resume,
        "build_cc_startup_context",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        cc_runtime_resume,
        "record_cc_startup_context",
        lambda *_args, **_kwargs: ("", []),
    )
    monkeypatch.setattr(cc_startup_process, "resolve_claude_dir", lambda _: "/tmp")
    # The replacement takes its root from the continuing Turn's account.
    monkeypatch.setattr("connections.account_runtime.thread_claude_dir",
                        lambda *_args, **_kwargs: "/tmp")
    monkeypatch.setattr(
        cc_startup_process,
        "build_starting_session",
        lambda *_args, **_kwargs: Session(),
    )
    monkeypatch.setattr(
        cc_startup_process, "install_starting_identity", install_identity,
    )
    monkeypatch.setattr(cc_runtime_helpers, "_validate_working_dir", lambda *_: None)
    launch_process = cc_startup_process.launch_cc_process

    async def launch_after_pause(*args, **kwargs):
        launch_paused.set()
        await release_launch.wait()
        return await launch_process(*args, **kwargs)

    monkeypatch.setattr(cc_runtime_resume, "launch_cc_process", launch_after_pause)

    _LIVE.reset()
    _LIVE.put(thread_id, old)
    task = asyncio.create_task(cc_runtime.resume_same_turn_after_checkpoint(
        thread_id,
        old,
        SimpleNamespace(
            runtime_context_epoch=4,
            replay=SimpleNamespace(tail=SimpleNamespace(resume_presence_line=None)),
        ),
    ))
    try:
        await asyncio.wait_for(launch_paused.wait(), timeout=0.5)
        assert _LIVE.pop(thread_id, None) is old
        old.turn_active = False
        old.corsia_turn = None
        # Stop waits for the replacement's lock, then finds nothing bound.
        stop = asyncio.create_task(
            cc_stop.stop(thread_id, expected_turn_id=identity.turn_id),
        )
        await asyncio.sleep(0)
        assert not stop.done()

        release_launch.set()
        with pytest.raises(RuntimeError, match="predecessor changed"):
            await asyncio.wait_for(task, timeout=0.5)
        assert await asyncio.wait_for(stop, timeout=0.5) is False
        assert starts == []
        assert identity_installs == []
        assert _LIVE.get(thread_id) is None
    finally:
        release_launch.set()
        await asyncio.gather(task, return_exceptions=True)
        _LIVE.reset()


@pytest.mark.asyncio
async def test_codex_checkpoint_replaces_attempt_beneath_same_turn(
    isolated_db,
    monkeypatch,
) -> None:
    from subagents import codex_runtime
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS, _LIVE, _LiveCodex

    identity = TurnIdentity("turn-codex", 7)
    terminated = []

    class _Session:
        binary = "codex"
        effort = "low"
        working_dir = "/tmp/project"
        project_id = "project-1"

        def __init__(self, name):
            self.name = name

        async def terminate(self) -> None:
            terminated.append(self.name)

    session = _Session("old")
    old = _LiveCodex(
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=identity,
        codex_thread_id="provider-thread",
        catalog_model_id="codex/test",
    )
    _LIVE.put("thread-codex", old)
    _APP_SESSIONS["thread-codex"] = session
    _CODEX_THREADS["thread-codex"] = "provider-thread"
    spawned: list[dict[str, object]] = []
    spawned_lives = []

    async def spawn_turn(*args, **kwargs):
        index = len(spawned) + 1
        spawned.append(
            dict(thread_id=args[0], current_user_text=args[1], **kwargs)
        )
        replacement_session = _Session(f"replacement-{index}")
        replacement = _LiveCodex(
            session=replacement_session,
            parent_thread_id=None,
            name="codex",
            corsia_turn=kwargs["preadmitted_turn"],
            codex_thread_id=f"replacement-provider-thread-{index}",
            catalog_model_id="codex/test",
        )
        _LIVE.put("thread-codex", replacement)
        _APP_SESSIONS["thread-codex"] = replacement_session
        _CODEX_THREADS["thread-codex"] = replacement.codex_thread_id
        spawned_lives.append(replacement)
        return kwargs["preadmitted_turn"]

    monkeypatch.setattr(codex_runtime, "_spawn_turn", spawn_turn)
    monkeypatch.setattr(codex_runtime, "_retire_registered_thread_artifacts", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: _target("codex"),
    )

    first = await codex_runtime.resume_same_turn_after_checkpoint(
        "thread-codex",
        old,
        SimpleNamespace(
            runtime_context_epoch=5,
            replay=SimpleNamespace(tail=SimpleNamespace(resume_presence_line=None)),
        ),
    )
    result = await codex_runtime.resume_same_turn_after_checkpoint(
        "thread-codex",
        spawned_lives[0],
        SimpleNamespace(
            runtime_context_epoch=6,
            replay=SimpleNamespace(tail=SimpleNamespace(resume_presence_line=None)),
        ),
    )

    assert first == identity
    assert result == identity
    assert terminated == ["old", "replacement-1"]
    assert [entry["preadmitted_turn"] for entry in spawned] == [identity, identity]
    assert [entry["successor_turn_id"] for entry in spawned] == [
        identity.turn_id,
        identity.turn_id,
    ]
    from history.adapters import MID_TURN_RESUME_ANCHOR

    assert [entry["current_user_text"] for entry in spawned] == [
        MID_TURN_RESUME_ANCHOR,
        MID_TURN_RESUME_ANCHOR,
    ]
    assert [
        context.runtime_context_epoch
        for entry in spawned
        if isinstance((context := entry["prepared_context"]), SimpleNamespace)
    ] == [5, 6]
    assert _LIVE.get("thread-codex") is spawned_lives[1]
    assert _CODEX_THREADS["thread-codex"] == "replacement-provider-thread-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_tool_boundary_carries_provider_checkpoint_identity(
    stub_recovery_measurement, isolated_db,
    monkeypatch,
    harness,
) -> None:
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        ProviderContinuation,
    )
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    identity = TurnIdentity(f"turn-{harness}", 3)
    target = _target(harness)
    boundary = HistoryBoundary("thread-1", 0, 4, "step-1")
    context = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=boundary,
        previous_generation_id=None,
        working_dir="/tmp",
        tail_tokens=12_000,
        minimum_tail_tokens=1_000,
        active_checkpoint=None,
        installed_checkpoint=SimpleNamespace(id="checkpoint-new", adopted_input_tokens=None),
        registered_checkpoint_id="checkpoint-old",
        previous=SimpleNamespace(id="checkpoint-new"),
    )
    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        session=SimpleNamespace(),
    )
    prepared = SimpleNamespace(
        rendered=SimpleNamespace(input_tokens=98_000),
        fit_ledger=SimpleNamespace(
            section_tokens=(
                ("current_input", 50),
                ("tools", 500),
                ("provider_wrapper", 20),
            )
        ),
    )
    captured = {}

    monkeypatch.setattr(
        bridge, "read_external_checkpoint_context", lambda *_a, **_k: context,
    )

    async def render(*_args, **_kwargs):
        return prepared

    monkeypatch.setattr(bridge, "render_external_selection", render)
    monkeypatch.setattr(bridge, "external_measurement", lambda *_a: (90_000, 100_000))

    async def maintain(provider_boundary, **options):
        captured["boundary"] = provider_boundary
        captured["options"] = options
        return ProviderContinuation(
            BoundaryDecision.ADOPT_AND_PROCEED,
            identity,
            payload=SimpleNamespace(runtime_context_epoch=2),
            checkpoint_id="checkpoint-new",
        )

    resumed = []

    async def resume(*args):
        resumed.append(args)

    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)
    monkeypatch.setattr(bridge, "_resume_external", resume)

    assert await bridge.maintain_external_tool_boundary(
        live,
        "thread-1",
        harness,
    ) is True
    assert captured["boundary"].turn == identity
    assert captured["boundary"].provider_input_tokens == 102_000
    assert captured["boundary"].canonical_input_tokens is None
    assert captured["boundary"].adopted_checkpoint_id == "checkpoint-old"
    assert captured["boundary"].installed_checkpoint.id == "checkpoint-new"
    assert captured["options"] == {"must_adopt": False}
    assert resumed[0][0:2] == (live, "thread-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_tool_boundary_renders_exact_when_usage_is_missing(
    isolated_db,
    monkeypatch,
    harness,
) -> None:
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    identity = TurnIdentity(f"turn-{harness}", 3)
    target = _target(harness)
    context = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=SimpleNamespace(start_exclusive=None, history_epoch=0, sealed_boundary=None),
        boundary=HistoryBoundary("thread-1", 0, 4, "step-1"),
        previous_generation_id=None,
        working_dir="/tmp",
        tail_tokens=12_000,
        minimum_tail_tokens=1_000,
    )
    reads = []
    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        session=SimpleNamespace(),
    )
    prepared = SimpleNamespace(
        rendered=SimpleNamespace(input_tokens=88_000),
        fit_ledger=SimpleNamespace(section_tokens=()),
    )
    rendered = []
    captured = {}

    def read_context(*_args, **kwargs):
        reads.append(kwargs.get("exact_history"))
        return context

    monkeypatch.setattr(bridge, "read_external_checkpoint_context", read_context)
    monkeypatch.setattr(bridge, "external_measurement", lambda *_a: (None, 100_000))

    async def render(*_args, **_kwargs):
        rendered.append(True)
        return prepared

    async def maintain(boundary, **options):
        captured["boundary"] = boundary
        captured["options"] = options
        return ProviderContinuation(BoundaryDecision.PROCEED, identity)

    monkeypatch.setattr(bridge, "render_external_selection", render)
    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)

    assert await bridge.maintain_external_tool_boundary(
        live,
        "thread-1",
        harness,
    ) is False
    assert rendered == [True]
    assert reads == [True, False]
    assert captured["boundary"].provider_input_tokens is None
    assert captured["boundary"].canonical_input_tokens == 88_000
    assert captured["options"] == {"must_adopt": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
@pytest.mark.parametrize(
    (
        "measurement",
        "rendered_tokens",
        "has_newer",
        "render_count",
        "replaces_handle",
    ),
    (
        pytest.param(None, 40_000, False, 1, False, id="pending-usage-fits"),
        pytest.param(None, 120_000, True, 1, True, id="pending-usage-over-window"),
        pytest.param(120_000, 40_000, False, 1, True, id="measured-over-window"),
    ),
)
async def test_external_tool_boundary_uses_the_context_that_seeded_its_handle(
    stub_recovery_measurement, isolated_db,
    monkeypatch,
    harness,
    measurement,
    rendered_tokens,
    has_newer,
    render_count,
    replaces_handle,
) -> None:
    """Pending usage uses checkpoint+tail; real pressure still replaces the handle."""
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from history.types import HistoryBoundary
    from subagents import harness_checkpoint_resume as bridge

    identity = TurnIdentity(f"turn-{harness}", 3)
    target = _target(harness)
    checkpoint_tail = object()
    installed = SimpleNamespace(id="checkpoint-current", status="installed", adopted_input_tokens=None)
    newer = (
        SimpleNamespace(id="checkpoint-next", status="running")
        if has_newer
        else None
    )
    context = bridge.ExternalCheckpointContext(
        thread_row={},
        target=target,
        history=object(),
        boundary=HistoryBoundary("thread-1", 0, 4, "step-1"),
        previous_generation_id=None,
        working_dir="/tmp",
        tail_tokens=1_000,
        minimum_tail_tokens=1_000,
        active_checkpoint=newer,
        installed_checkpoint=installed,
        registered_checkpoint_id=installed.id,
    )
    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        session=SimpleNamespace(),
    )
    prepared = SimpleNamespace(
        runtime_context_epoch=4,
        rendered=SimpleNamespace(input_tokens=rendered_tokens),
        fit_ledger=SimpleNamespace(section_tokens=()),
    )
    rendered = []
    resumed = []

    monkeypatch.setattr(
        bridge, "read_external_checkpoint_context", lambda *_a, **_k: context,
    )
    monkeypatch.setattr(
        bridge,
        "external_measurement",
        lambda *_a: (measurement, 100_000),
    )
    monkeypatch.setattr(
        "core.checkpoint_resume.installed_replay",
        lambda record: checkpoint_tail if record is installed else None,
    )

    async def render(_context, _identity, selection, _input_identity):
        rendered.append(selection)
        return prepared

    async def await_newer(boundary, record, **_kwargs):
        assert boundary.installed_checkpoint is None
        assert record is newer
        return ProviderContinuation(
            BoundaryDecision.ADOPT_AND_PROCEED,
            identity,
            payload=prepared,
            checkpoint_id=record.id,
        )

    async def resume(*args):
        resumed.append(args)

    monkeypatch.setattr(bridge, "render_external_selection", render)
    monkeypatch.setattr("core.checkpoint_resume._await_installed", await_newer)
    monkeypatch.setattr(bridge, "_resume_external", resume)

    assert await bridge.maintain_external_tool_boundary(
        live,
        "thread-1",
        harness,
    ) is replaces_handle
    assert rendered == [checkpoint_tail] * render_count
    assert bool(resumed) is replaces_handle
    if resumed:
        assert resumed == [(live, "thread-1", prepared, harness)]
