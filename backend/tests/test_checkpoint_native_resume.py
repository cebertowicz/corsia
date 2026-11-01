"""Native checkpoint adoption stays beneath one logical CORSIA Turn."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.continuity_accounting import ContinuityAccounting


def _response(*, tools: bool, content: str):
    tool_calls = [SimpleNamespace(id="tool-1", name="read", arguments={})] if tools else []
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        has_tool_calls=tools,
        finish_reason="stop",
        usage={"prompt_tokens": 10},
        ttft_ms=1,
    )


def _ledger(rendered: int = 10):
    return SimpleNamespace(
        target_catalog_id="native/test",
        effective_window_tokens=100_000,
        rendered_input_tokens=rendered,
        total_tokens=rendered,
        input_identity="input-1",
        section_tokens=(
            ("fixed", rendered),
            ("checkpoint", 0),
            ("tail", 0),
            ("current_input", 0),
            ("tools", 0),
            ("provider_wrapper", 0),
        ),
        fits=True,
    )


def test_native_checkpoint_pressure_counts_cached_prefix_from_normalized_usage():
    from core.turn_iteration_context import _correlated_native_input
    from infra.provider_usage import TokenUsage

    state = SimpleNamespace(
        last_measured_call_usage=TokenUsage(
            input_uncached=4_000,
            cache_read=18_000,
        ).ledger_row(),
        last_measured_rendered_input_tokens=23_000,
        fit_ledger=_ledger(31_000),
    )

    assert _correlated_native_input(state) == 30_000


@pytest.mark.asyncio
async def test_native_live_warm_source_freezes_correlated_provider_receipt(monkeypatch):
    from core import checkpoint_native_admission
    from history.types import HistoryBoundary
    from infra.provider_usage import TokenUsage

    boundary = HistoryBoundary("thread-1", 0, 3, "result-1")
    state = SimpleNamespace(
        checkpoint_source_messages=[{"role": "user", "content": "accepted"}],
        tool_definitions=[{"type": "function", "function": {"name": "read"}}],
        last_measured_call_usage=TokenUsage(
            input_uncached=4_000,
            cache_read=18_000,
        ).ledger_row(),
        last_measured_rendered_input_tokens=23_000,
    )
    context = SimpleNamespace(
        boundary=boundary,
        target=SimpleNamespace(preserve_thinking_default=True),
    )
    captured = {}

    def capture(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("core.fork_native_warm.capture_live_warm_source", capture)

    result = await checkpoint_native_admission.capture_native_checkpoint_source(
        "thread-1", state, context, "high",
    )

    assert result is not None
    assert captured["kwargs"]["provider_input_tokens"] == 22_000
    assert captured["kwargs"]["rendered_input_tokens"] == 23_000


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["initial", "safe-boundary", "none"])
async def test_native_adoption_preserves_only_unaccepted_input_and_resets_occupancy(
    monkeypatch, pending,
):
    from core.turn_iteration import _IterationState
    from core.turn_iteration_context import _render_native_installed, adopt_native_context

    frame = {"role": "user", "content": [
        {"type": "text", "text": "pending message"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]}
    settled_tail = [{"role": "assistant", "content": "accepted result"}]
    state = _IterationState(
        messages=[*settled_tail, frame], context_usage={}, tool_definitions=[],
        context_identity=None, fit_ledger=_ledger(), fit_current_input=frame,
        checkpoint_source_messages=settled_tail, turn_model="native/test",
        provider_delivery_settled=pending != "initial",
        active_boundary_input=object() if pending == "safe-boundary" else None,
        last_measured_call_usage={"prompt_tokens": 90_000},
        last_measured_rendered_input_tokens=80_000,
    )
    selection = SimpleNamespace(tail=SimpleNamespace(messages=()))
    monkeypatch.setattr("core.checkpoint_resume.installed_replay", lambda _: selection)

    async def prefix(**_kwargs):
        return list(settled_tail), {
            "continuity_accounting": ContinuityAccounting.not_delivered(),
        }

    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", prefix)
    monkeypatch.setattr("core.destination_preflight.attach_native_provider_fit_ledger",
                        lambda **_kw: SimpleNamespace(fit_ledger=_ledger()))
    continuation = await _render_native_installed(
        SimpleNamespace(id="replacement"), agent=object(), state=state,
        thread_id="thread-1", turn_identity=SimpleNamespace(turn_id="turn-1"),
        turn_model="native/test", project_id=None,
    )
    adopt_native_context(state, continuation.payload)
    assert state.messages == settled_tail + ([frame] if pending != "none" else [])
    assert state.last_measured_call_usage is None
    assert state.last_measured_rendered_input_tokens is None
    assert state.checkpoint_source_messages == []


@pytest.mark.asyncio
async def test_unaccepted_initial_input_is_not_a_warm_checkpoint_source():
    from core.turn_iteration import _prepare_iteration_state
    from core.turn_context import NativePreparedContext

    messages = ({"role": "system", "content": "accepted history"},
                {"role": "user", "content": "not accepted yet"})
    prepared = NativePreparedContext(messages, {}, (), None, _ledger())
    state = await _prepare_iteration_state(
        None, "thread-1", "not accepted yet", "native/test", False,
        None, "", "", None, 100_000, prepared,
    )
    assert state.messages == list(messages)
    assert state.checkpoint_source_messages == []


@pytest.mark.asyncio
async def test_native_initial_pressure_falls_back_to_cold_checkpoint(monkeypatch):
    """No receipt means cold capture, not an unavailable checkpoint wall."""
    from core import checkpoint_native_admission

    async def capture(*_args, **_kwargs):
        return None

    built = object()

    async def run_off_db(function, *args):
        assert function is checkpoint_native_admission.build_native_cold_checkpoint_admission
        assert args[0] == "checkpoint-1"
        return built

    monkeypatch.setattr(
        checkpoint_native_admission, "capture_native_checkpoint_source", capture,
    )
    monkeypatch.setattr(checkpoint_native_admission, "run_off_db", run_off_db)

    result = await checkpoint_native_admission.prepare_native_checkpoint(
        "checkpoint-1",
        "thread-1",
        SimpleNamespace(context_usage={}),
        SimpleNamespace(),
        SimpleNamespace(turn_id="turn-1"),
        None,
    )

    assert result is built


def test_native_checkpoint_matches_selected_transport_to_catalog_target(isolated_db):
    import json

    from core.turn_iteration_context import _read_native_checkpoint_context
    from infra.database import get_db

    db = get_db()
    db.execute(
        "UPDATE models SET litellm_model='omlx@local/qwen-transport' "
        "WHERE id='oMLX/qwen'"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('running-native','steve','omlx@local/qwen-transport','t','t')"
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-running','running-native',0,'omlx@local/qwen-transport',"
        "'running','t','boot')"
    )
    rows = (
        ("u1", "user", "start", {"turn_id": "turn-running"}),
        ("a1", "assistant", "first", {
            "turn_id": "turn-running",
            "llm_tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        }),
        ("r1", "tool", "first result", {
            "turn_id": "turn-running", "tool_call_id": "call-1",
            "assistant_step_id": "a1", "name": "Read",
        }),
        ("a2", "assistant", "second", {
            "turn_id": "turn-running",
            "llm_tool_calls": [{
                "id": "call-2", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        }),
        ("r2", "tool", "second result", {
            "turn_id": "turn-running", "tool_call_id": "call-2",
            "assistant_step_id": "a2", "name": "Read",
        }),
    )
    db.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'running-native',?,?,?,'t',?)",
        [
            (message_id, role, content, json.dumps(metadata), seq)
            for seq, (message_id, role, content, metadata) in enumerate(rows, 1)
        ],
    )
    db.commit()
    db.close()

    context = _read_native_checkpoint_context("running-native", "oMLX/qwen")

    assert context is not None
    assert context.target.catalog_id == "oMLX/qwen"
    assert context.target.transport_model == "omlx@local/qwen-transport"
    assert context.boundary.seq == 3
    assert context.boundary.message_id == "r1"


@pytest.mark.asyncio
async def test_native_adopts_each_new_installed_generation_once(monkeypatch):
    """One Turn replaces its context once for each newly installed checkpoint."""
    from core import checkpoint_resume, turn_iteration_context
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from core.turn_core import TurnIdentity

    identity = TurnIdentity("turn-native", 7)
    current = {"id": "checkpoint-1"}
    observed: list[str | None] = []
    selection = SimpleNamespace(tail=SimpleNamespace(messages=()))

    async def maintain(boundary, **options):
        assert options == {"must_adopt": False}
        adopted = boundary.adopted_checkpoint_id
        assert boundary.installed_checkpoint.id == current["id"]
        observed.append(adopted)
        payload = None
        if adopted != current["id"]:
            rendered = await boundary.render_installed(boundary.installed_checkpoint)
            payload = rendered.payload
        return ProviderContinuation(
            BoundaryDecision.ADOPT_AND_PROCEED if payload else BoundaryDecision.PROCEED,
            identity,
            payload=payload,
            checkpoint_id=current["id"] if payload else None,
        )

    monkeypatch.setattr(checkpoint_resume, "maintain_before_provider_call", maintain)
    monkeypatch.setattr(checkpoint_resume, "installed_replay", lambda _record: selection)

    async def build_prefix(**kwargs):
        assert kwargs["replay_selection"] is selection
        return (
            [{"role": "system", "content": current["id"]}],
            {
                "installed_context": f"delivery:{current['id']}",
                "continuity_accounting": ContinuityAccounting.not_delivered(),
            },
        )

    retained: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "core.installed_context.persist_prepared_installed_context",
        lambda thread_id, prepared: retained.append((thread_id, prepared.installed)),
    )

    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", build_prefix)
    monkeypatch.setattr(
        "core.destination_preflight.attach_native_provider_fit_ledger",
        lambda **_kwargs: SimpleNamespace(fit_ledger=_ledger()),
    )

    async def checkpoint_snapshot(function, *args):
        if function is turn_iteration_context.provider_boundary_snapshot:
            installed = SimpleNamespace(id=current["id"])
            return None, installed, installed
        return function(*args)

    monkeypatch.setattr("llm.resolution.resolve_locality_permitted",
                        lambda _model, **_k: SimpleNamespace(context_window=100_000, context_window_source="provider"))
    monkeypatch.setattr(turn_iteration_context, "run_in_db", checkpoint_snapshot)
    state = SimpleNamespace(
        messages=[{"role": "user", "content": "continue"}],
        context_usage={},
        tool_definitions=[],
        context_identity=None,
        fit_ledger=_ledger(),
        fit_current_input={"role": "user", "content": "continue"},
        checkpoint_source_messages=[],
        provider_delivery_settled=True,
        active_boundary_input=None,
        last_measured_call_usage=None,
        last_measured_rendered_input_tokens=None,
        overflow_retry_pending=False,
    )
    cancel = SimpleNamespace(checkpoint_wait_scope=CheckpointWaitScope())

    for generation_id in (
        "checkpoint-1", "checkpoint-1", "checkpoint-2", "checkpoint-2",
    ):
        current["id"] = generation_id
        prepared = await turn_iteration_context.maintain_native_checkpoint(
            agent=SimpleNamespace(),
            state=state,
            thread_id="thread-native",
            turn_identity=identity,
            cancel_event=cancel,
            turn_model="native/test",
            reasoning_effort=None,
            project_id=None,
        )
        if prepared is not None:
            turn_iteration_context.adopt_native_context(state, prepared)

    assert observed == [None, "checkpoint-1", "checkpoint-1", "checkpoint-2"]
    # Each adopted continuation retains the static rows it delivers.
    assert retained == [
        ("thread-native", "delivery:checkpoint-1"),
        ("thread-native", "delivery:checkpoint-2"),
    ]


@pytest.mark.asyncio
async def test_native_rebuilds_twice_beneath_same_turn_instead_of_cancelling(
    monkeypatch,
):
    from core import turn_iteration, turn_iteration_context, turn_provider_call, turns_steps
    from core.native_turn_control import TurnCancelEvent
    from core.turn_context import NativePreparedContext
    from core.turn_core import TurnIdentity

    identity = TurnIdentity("turn-native", 7)
    cancel = TurnCancelEvent()
    cancel.bind_turn_identity(identity)
    calls: list[list[dict]] = []
    boundaries: list[TurnIdentity] = []
    responses = iter((
        (_response(tools=True, content="calling"), False),
        (_response(tools=True, content="calling-again"), False),
        (_response(tools=False, content="finished"), False),
    ))

    async def call_provider(*args, **_kwargs):
        calls.append([dict(message) for message in args[1]])
        return next(responses)

    async def execute_tools(*_args, **_kwargs):
        return [
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "calling", "tool_calls": []},
            {"role": "tool", "tool_call_id": "tool-1", "content": "large"},
        ]

    async def maintain(*_args, turn_identity, state, **_kwargs):
        boundaries.append(turn_identity)
        if len(boundaries) == 1:
            return None
        return NativePreparedContext(
            messages=({
                "role": "system",
                "content": f"checkpoint-seed-{len(boundaries) - 1}",
            },),
            context_usage={"continuity_mode": "checkpoint_tail"},
            tool_definitions=(),
            context_identity=None,
            fit_ledger=_ledger(5),
        )

    async def no_db(*_args, **_kwargs):
        return None

    monkeypatch.setattr(turn_provider_call, "call_llm", call_provider)
    monkeypatch.setattr(turns_steps, "execute_tool_calls", execute_tools)
    monkeypatch.setattr(turns_steps, "emit_reasoning", no_db)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", lambda _sid: no_db())
    monkeypatch.setattr(turn_iteration_context, "refit_provider_payload", lambda *_a, **_k: _ledger(100_001))
    monkeypatch.setattr(turn_iteration_context, "maintain_native_checkpoint", maintain)
    monkeypatch.setattr(turn_iteration, "run_in_db", no_db)

    events: list[str] = []

    async def on_step(kind, _payload):
        events.append(kind)

    agent = SimpleNamespace(
        model="native/test",
        max_iterations=4,
        _connect_mcp=no_db,
        _strip_think=lambda value: value,
    )
    prepared = NativePreparedContext(
        messages=({"role": "user", "content": "original"},),
        context_usage={},
        tool_definitions=(),
        context_identity=None,
        fit_ledger=_ledger(),
    )

    await turn_iteration.run_native_iteration(
        agent,
        "original",
        "thread-native",
        "web",
        "thread-native",
        on_step,
        cancel,
        model="test",
        turn_provider={},
        model_context_window=100_000,
        prepared_context=prepared,
    )

    assert boundaries == [identity, identity, identity]
    assert calls[1] == [{"role": "system", "content": "checkpoint-seed-1"}]
    assert calls[2] == [{"role": "system", "content": "checkpoint-seed-2"}]
    assert events.count("context_usage") == 3
    assert events[-2:] == ["text", "turn_complete"]
    assert cancel.is_set() is False


@pytest.mark.asyncio
async def test_input_bar_stop_wins_over_awaited_checkpoint_cancellation(
    monkeypatch,
):
    """Stop must cancel the Turn, not publish its checkpoint's exit as error."""
    from core import turn_iteration, turn_iteration_context, turns_steps
    from core.checkpoint_resume import CheckpointContinuationError
    from core.native_turn_control import TurnCancelEvent
    from core.turn_context import NativePreparedContext
    from core.turn_core import TurnIdentity

    identity = TurnIdentity("turn-stopped-at-checkpoint", 1)
    cancel = TurnCancelEvent()
    cancel.bind_turn_identity(identity)
    checkpoint_waiting = asyncio.Event()
    events: list[str] = []

    async def maintain(**_kwargs):
        checkpoint_waiting.set()
        await cancel.wait()
        raise CheckpointContinuationError("checkpoint ended cancelled")

    async def no_db(*_args, **_kwargs):
        return None

    async def on_step(kind, _payload):
        events.append(kind)

    monkeypatch.setattr(turn_iteration_context, "maintain_native_checkpoint", maintain)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", lambda _sid: no_db())
    monkeypatch.setattr(turn_iteration, "run_in_db", no_db)
    agent = SimpleNamespace(
        max_iterations=1,
        _connect_mcp=no_db,
        _strip_think=lambda value: value,
    )
    prepared = NativePreparedContext(
        messages=({"role": "user", "content": "original"},),
        context_usage={},
        tool_definitions=(),
        context_identity=None,
        fit_ledger=_ledger(),
    )
    running = asyncio.create_task(turn_iteration.run_native_iteration(
        agent,
        "original",
        "thread-native-stop",
        "web",
        "thread-native-stop",
        on_step,
        cancel,
        model="test",
        turn_provider={},
        prepared_context=prepared,
    ))
    await checkpoint_waiting.wait()
    cancel.user_stop_requested = True
    cancel.set()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert events == ["turn_cancelled"]
