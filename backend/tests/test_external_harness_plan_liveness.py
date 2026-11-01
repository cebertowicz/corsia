"""Native, Claude Code, and Codex shared structured turn-completion hook."""

from __future__ import annotations

import json

from automation import plan_liveness_terminal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from core.turn_core import BOOT_EPOCH, TurnIdentity
from infra.failure_reference import REFERENCE
from infra.database import get_db
from plans import admission as plan_admission, service
from tests.event_routing_helpers import mk_session


def _bind_active_turn(live, thread_id: str, *, db=None, metadata=None) -> None:
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    turn_id = f"turn-{thread_id}"
    admission = admit_input(
        thread_id,
        role="user",
        content="test",
        intent="apply_now",
        message_id=f"input-{thread_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at="t",
        metadata=metadata,
        db=db,
    )
    assert admission.batch is not None
    begin_batch_write(
        admission.batch,
        attempt_id=f"write-{thread_id}",
        started_at="t",
        db=db,
    )
    settle_batch_write(f"write-{thread_id}", outcome="accepted", db=db)
    live.corsia_turn = admission.batch.identity


async def _deliver_codex_terminal(handler, event: dict, live) -> None:
    await handler(event)
    if live.delivery_event_tail is not None:
        await live.delivery_event_tail


@pytest.mark.parametrize(
    ("event", "expected_calls"),
    [
        ({"kind": "result", "is_error": False}, 1),
        ({"kind": "result", "is_error": True}, 1),
        ({"kind": "result", "stop_reason": "interrupted"}, 0),
        ({"kind": "turn_failed", "cause": "process_exited"}, 1),
    ],
)
def test_codex_terminal_calls_the_shared_hook_unless_interrupted(
    isolated_db, event, expected_calls, caplog,
):
    """Success, an errored result and ``turn_failed`` all reach plan liveness,
    the errors with ``outcome='error'`` so a stuck child wakes its delegating
    parent (F1), matching the Claude Code seam. Only an interrupt skips it."""
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = mk_session(f"codex-plan-{expected_calls}-{event['kind']}")
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
    )
    _bind_active_turn(live, thread_id)
    completion = MagicMock()

    sink = logger.add(caplog.handler, level="INFO", format="{message}")
    try:
        with patch(
            "automation.plan_liveness_terminal.on_thread_turn_complete",
            completion,
        ):
            _run(
                _deliver_codex_terminal(
                    codex_runtime._make_on_event(live, thread_id, MagicMock()),
                    event,
                    live,
                )
            )
    finally:
        logger.remove(sink)

    assert completion.call_count == expected_calls
    if expected_calls:
        assert completion.call_args.args == (thread_id,)
    else:
        db = get_db()
        try:
            assert db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id=? AND "
                "json_extract(metadata, '$.type') IN (?,?,?,?)",
                (thread_id, "llm_error", "cc_error", "tool_boundary_violation", "tool_bridge_unavailable"),
            ).fetchone()[0] == 0
        finally:
            db.close()
        assert not [record for record in caplog.records if "turn_error " in record.getMessage()]


def test_codex_turn_failed_carries_the_error_text_to_plan_liveness(isolated_db):
    """F1 live incident: an auth/session-limit Turn failure must name itself.

    A Turn that ends with a provider auth or session error is F1, not a
    silent stop -- the parent's wake must carry the actual error text, never
    a generic 'the worker stopped'.
    """
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = mk_session("codex-plan-auth-error")
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
    )
    _bind_active_turn(live, thread_id)
    completion = MagicMock()

    with patch(
        "automation.plan_liveness_terminal.on_thread_turn_complete", completion,
    ):
        _run(
            _deliver_codex_terminal(
                codex_runtime._make_on_event(live, thread_id, MagicMock()),
                {
                    "kind": "turn_failed",
                    "cause": "auth_failed",
                    "message": "Not logged in · Please run /login",
                },
                live,
            )
        )

    assert completion.call_args.kwargs["outcome"] == "error"
    assert "not logged in" in completion.call_args.kwargs["error"].lower()
    assert completion.call_args.kwargs["turn_id"]


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "turn_failed", "cause": "process_exited"},
        {"kind": "result", "is_error": True, "result": "boom"},
    ],
)
def test_codex_error_marker_is_recorded_before_plan_liveness(isolated_db, event, caplog):
    """A plan-side failure must never hide the user-visible failure marker."""
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime, codex_runtime_events

    _reset_runtime()
    thread_id = mk_session(f"codex-plan-order-{event['kind']}")
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
    )
    _bind_active_turn(live, thread_id)
    order: list[str] = []
    real_marker = codex_runtime_events.record_terminal_marker

    async def marker(*args, **kwargs):
        order.append("marker")
        return await real_marker(*args, **kwargs)

    sink = logger.add(caplog.handler, level="INFO", format="{message}")
    try:
        with patch.object(codex_runtime_events, "record_terminal_marker", marker), patch(
            "automation.plan_liveness_terminal.on_thread_turn_complete",
            MagicMock(side_effect=lambda *a, **k: order.append("plan")),
        ):
            _run(
                _deliver_codex_terminal(
                    codex_runtime._make_on_event(live, thread_id, MagicMock()),
                    event,
                    live,
                )
            )
    finally:
        logger.remove(sink)

    assert order == ["marker", "plan"]
    if event["kind"] != "turn_failed":
        return
    db = get_db()
    try:
        metadata = json.loads(db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='system' "
            "AND json_extract(metadata,'$.type')='cc_error' ORDER BY seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()["metadata"])
    finally:
        db.close()
    assert metadata["type"] == "cc_error"
    assert metadata["turn_id"] == live.corsia_turn.turn_id
    assert len(metadata["reference"]) == 6 and REFERENCE.fullmatch(metadata["reference"])
    lines = [record.getMessage() for record in caplog.records if "turn_error " in record.getMessage()]
    assert lines == [
        f"turn_error reference={metadata['reference']} turn_id={live.corsia_turn.turn_id} "
        f"thread={thread_id} kind=cc_error",
    ]


def test_codex_terminal_surfaces_plan_completion_failure(isolated_db):
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = mk_session("codex-plan-write-failure")
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread",
        session=None,
        parent_thread_id=None,
        name="codex",
    )
    _bind_active_turn(live, thread_id)
    completion = MagicMock(side_effect=RuntimeError("plan write failed"))

    with patch(
        "automation.plan_liveness_terminal.on_thread_turn_complete", completion
    ), pytest.raises(RuntimeError, match="plan write failed"):
        _run(
            _deliver_codex_terminal(
                codex_runtime._make_on_event(live, thread_id, MagicMock()),
                {"kind": "result", "is_error": False},
                live,
            )
        )


@pytest.mark.parametrize(
    ("event", "interrupt_requested", "expected_calls"),
    [
        ({"kind": "result", "is_error": False}, False, 1),
        ({"kind": "result", "is_error": True}, False, 1),
        ({"kind": "result", "stop_reason": "interrupted"}, False, 0),
        ({"kind": "result", "is_error": False}, True, 0),
    ],
)
@pytest.mark.asyncio
async def test_cc_terminal_calls_the_shared_hook_unless_the_parent_interrupted(
    isolated_db, event, interrupt_requested, expected_calls
):
    """An errored (non-interrupted) result still calls the shared hook (F1):
    the shared hook must reach plan liveness with ``outcome='error'`` so a
    child stuck on an error can wake its delegating parent -- only a Turn the
    *parent* interrupted stays silent."""
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    thread_id = mk_session(
        f"cc-plan-{expected_calls}-{int(interrupt_requested)}-{event.get('is_error')}"
    )
    live = _Live(session=None, parent_thread_id=None, name="cc")
    live.turn_active = True
    live.interrupt_requested = interrupt_requested
    completion = MagicMock()
    db = get_db()
    try:
        _bind_active_turn(live, thread_id, db=db)
        with patch(
            "automation.plan_liveness_terminal.on_thread_turn_complete",
            completion,
        ):
            await _make_on_event(live, thread_id, MagicMock(), db)(event)
    finally:
        db.close()

    assert completion.call_count == expected_calls
    if expected_calls:
        assert completion.call_args.args == (thread_id,)


@pytest.mark.asyncio
async def test_cc_error_result_carries_the_error_text_to_plan_liveness(isolated_db):
    """F1 live incident, Claude Code side: an errored result names itself too."""
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    thread_id = mk_session("cc-plan-auth-error")
    live = _Live(session=None, parent_thread_id=None, name="cc")
    live.turn_active = True
    live.interrupt_requested = False
    completion = MagicMock()
    db = get_db()
    try:
        _bind_active_turn(live, thread_id, db=db)
        with patch(
            "automation.plan_liveness_terminal.on_thread_turn_complete",
            completion,
        ):
            await _make_on_event(live, thread_id, MagicMock(), db)({
                "kind": "result",
                "is_error": True,
                "result": "Not logged in · Please run /login",
            })
    finally:
        db.close()

    assert completion.call_args.kwargs["outcome"] == "error"
    assert "not logged in" in completion.call_args.kwargs["error"].lower()
    assert completion.call_args.kwargs["turn_id"]


@pytest.mark.asyncio
async def test_cc_terminal_surfaces_plan_completion_failure(isolated_db):
    from subagents.cc_process_exit import _CommittedTerminalFailure
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    thread_id = mk_session("cc-plan-write-failure")
    live = _Live(session=None, parent_thread_id=None, name="cc")
    live.turn_active = True
    completion = MagicMock(side_effect=RuntimeError("plan write failed"))
    db = get_db()
    try:
        _bind_active_turn(live, thread_id, db=db)
        with patch(
            "automation.plan_liveness_terminal.on_thread_turn_complete", completion
        ), pytest.raises(_CommittedTerminalFailure, match="plan write failed") as failure:
            await _make_on_event(live, thread_id, MagicMock(), db)(
                {"kind": "result", "is_error": False}
            )
        assert isinstance(failure.value.__cause__, RuntimeError)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("step_type", "payload", "expected_calls"),
    [
        ("turn_complete", {}, 1),
        ("error", {"message": "failed"}, 1),
        ("turn_cancelled", {"_replacement_requested": True}, 0),
    ],
)
@pytest.mark.asyncio
async def test_native_terminal_calls_the_shared_hook_unless_cancelled(
    isolated_db, monkeypatch, step_type, payload, expected_calls, caplog,
):
    """F1: a native/local Turn that errors still reaches plan liveness, via
    ``_handle_error``'s own call rather than the turn-complete handler's --
    only a cancelled Turn (never an error) stays silent."""
    from core import step_handler as step_module

    thread_id = mk_session(f"native-plan-{step_type}")
    handler = step_module.StepHandler(
        thread_id,
        {"model": "test/model", "model_name": "Test"},
        AsyncMock(),
        f"turn-{thread_id}",
    )
    handler.bind_turn_identity(TurnIdentity(f"turn-{thread_id}", 1))

    async def immediate(callable_, *args, **kwargs):
        return callable_(*args, **kwargs)

    monkeypatch.setattr(step_module, "run_in_db", immediate)
    completion = MagicMock()
    sink = logger.add(caplog.handler, level="INFO", format="{message}")
    try:
        with patch("automation.plan_liveness.on_turn_complete", completion):
            await handler.handle(step_type, payload)
    finally:
        logger.remove(sink)

    assert completion.call_count == expected_calls
    if expected_calls:
        assert completion.call_args.args == (thread_id,)
    else:
        db = get_db()
        try:
            assert db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id=? AND "
                "json_extract(metadata, '$.type') IN (?,?,?,?)",
                (thread_id, "llm_error", "cc_error", "tool_boundary_violation", "tool_bridge_unavailable"),
            ).fetchone()[0] == 0
        finally:
            db.close()
        assert not [record for record in caplog.records if "turn_error " in record.getMessage()]


@pytest.mark.asyncio
async def test_native_llm_error_frame_matches_history_projection(isolated_db, monkeypatch):
    from api.thread_paging import finish_display
    from core import step_handler as step_module

    thread_id = mk_session("native-error-reference-parity")
    emit = AsyncMock()
    handler = step_module.StepHandler(
        thread_id,
        {"model": "test/model", "model_name": "Test"},
        emit,
        f"turn-{thread_id}",
    )
    handler.bind_turn_identity(TurnIdentity(f"turn-{thread_id}", 1))

    async def immediate(callable_, *args, **kwargs):
        return callable_(*args, **kwargs)

    monkeypatch.setattr(step_module, "run_in_db", immediate)
    await handler.handle("error", {"message": "provider failed"})

    frame = next(call.args[0] for call in emit.await_args_list if call.args[0]["type"] == "error")
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, metadata FROM messages WHERE thread_id=? AND role='system' "
            "ORDER BY seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        history = [{"id": row["id"], "metadata": row["metadata"]}]
        finish_display(db, thread_id, history, include_tool_results=True)
    finally:
        db.close()

    assert frame["turn_id"] == history[0]["metadata"]["turn_id"]
    assert frame["reference"] == history[0]["metadata"]["reference"]
    assert frame["next_step"] == history[0]["metadata"]["next_step"]


@pytest.mark.asyncio
async def test_native_error_carries_the_error_text_to_plan_liveness(
    isolated_db, monkeypatch,
):
    """F1 live incident, native side: an errored step names itself too."""
    from automation import plan_liveness
    from core import step_handler as step_module

    thread_id = mk_session("native-plan-auth-error")
    handler = step_module.StepHandler(
        thread_id,
        {"model": "test/model", "model_name": "Test"},
        AsyncMock(),
        f"turn-{thread_id}",
    )
    handler.bind_turn_identity(TurnIdentity(f"turn-{thread_id}", 1))

    async def immediate(callable_, *args, **kwargs):
        return callable_(*args, **kwargs)

    monkeypatch.setattr(step_module, "run_in_db", immediate)
    completion = MagicMock()
    with patch.object(plan_liveness_terminal, "on_thread_turn_complete", completion):
        await handler.handle("error", {"message": "Not logged in · Please run /login"})

    assert completion.call_args.kwargs["outcome"] == "error"
    assert "not logged in" in completion.call_args.kwargs["error"].lower()
    assert completion.call_args.kwargs["turn_id"] == handler.turn_id


def test_shared_completion_arms_structured_actionable_plan(isolated_db):
    from automation import plan_liveness

    thread_id = mk_session("shared-plan-completion")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Continue",
        acceptance_criteria="Continuation test passes",
        db=db,
    )
    service.transition_node(node.id, "active", db=db)
    plan_admission.select(
        node.id, actor_id=thread_id, provenance="test", db=db, touch=False,
    )

    wake = plan_liveness.on_turn_complete(thread_id, db=db)

    assert wake is not None
    assert wake.thread_id == thread_id
    assert wake.last_open_count == 1
    assert len(wake.last_signal_hash) == 64
