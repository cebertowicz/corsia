"""Pure initial-send selection before the live Turn owns checkpoint waits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, status: str, mode: str, tokens: int):
        self.status = status
        self.mode = mode
        self.prepared_context = SimpleNamespace(
            mode=mode,
            rendered=SimpleNamespace(input_tokens=tokens),
        )

    def to_dict(self):
        return {
            "status": self.status,
            "continuity_mode": self.mode,
            "prepared_context": self.prepared_context,
        }


def _candidates(*, checkpoint_tail=None):
    return SimpleNamespace(
        exact="exact",
        checkpoint_tail=checkpoint_tail,
        sealed_boundary=object(),
        history_epoch=0,
    )


def _target():
    return SimpleNamespace(catalog_id="claude-code/sonnet")


@pytest.mark.parametrize(
    ("provider", "truth", "expected"),
    (
        ("cc", {"cc_live": True, "cc_has_session": False, "busy": True}, True),
        ("cc", {"cc_live": True, "cc_has_session": False, "busy": False}, False),
        ("codex", {"codex_live": True, "codex_has_thread": False, "busy": True}, True),
        ("codex", {"codex_live": True, "codex_has_thread": False, "busy": False}, False),
    ),
)
def test_retired_registration_preserves_only_active_turn_steer(
    provider: str,
    truth: dict,
    expected: bool,
) -> None:
    """Retirement changes the next idle send, not this Turn's steer path."""
    from infra.send_preflight import _provider_resume_available

    assert _provider_resume_available(
        provider,
        thread_row={"model": "claude-code/sonnet"},
        target=_target(),
        runtime_truth=truth,
    ) is expected


@pytest.mark.asyncio
async def test_installed_selection_refresh_preserves_exact_claimed_input(
    monkeypatch,
) -> None:
    """Checkpoint adoption re-renders the frozen input and media, not resume."""
    from infra import input_render, runtime_truth, send_dispatch
    from infra import send_preflight
    from infra.queries import load_thread_row

    media = object()
    envelope = object()
    projection = input_render.InputProviderProjection(
        "projection-1",
        "provider input",
        "source input",
        envelope,
        ({"file_id": "file-1"},),
        media,
        (),
    )
    batch = SimpleNamespace(thread_id="thread-1", turn_id="turn-1")
    target = SimpleNamespace(catalog_id="claude-code/sonnet", harness="claude-code")
    original = SimpleNamespace(mode="provider_resume")
    adopted = SimpleNamespace(mode="checkpoint_tail")
    selection = SimpleNamespace(checkpoint_generation_id="checkpoint-1")
    captured = {}

    async def truth(*_args, **_kwargs):
        return {"addressable": True, "execution_class": "cc_main"}

    async def in_db(fn, *_args, **_kwargs):
        from llm.resolution import resolve_execution_target

        if fn is resolve_execution_target:
            return target
        assert fn is load_thread_row
        return {"id": "thread-1", "user_id": "user-1", "runtime_context_epoch": 2}

    async def load_projection(*_args, **_kwargs):
        return projection

    async def preflight(**_kwargs):
        return {"prepared_context": original}

    class Attempt:
        async def cold(self, received, *, trigger_reason):
            assert received is selection
            assert trigger_reason is None
            return SimpleNamespace(prepared_context=adopted)

    def attempt(**kwargs):
        captured.update(kwargs)
        return Attempt()

    monkeypatch.setattr(runtime_truth, "read_runtime_truth", truth)
    monkeypatch.setattr(input_render, "run_in_db", in_db)
    monkeypatch.setattr(input_render, "_load_input_projection_async", load_projection)
    monkeypatch.setattr(send_dispatch, "provider_for", lambda _class: ("cc", False))
    monkeypatch.setattr(send_preflight, "context_preflight", preflight)
    monkeypatch.setattr(send_preflight, "_destination_attempt", attempt)

    prepared = await input_render.prepare_claimed_batch(
        batch,
        thread_row={"user_id": "user-1"},
        target=target,
    )
    refreshed = await prepared.refresh_prepared_context(selection)

    assert refreshed is adopted
    assert captured["current_user_content"] == "provider input"
    assert captured["current_user_source_content"] == "source input"
    assert captured["agent_envelope"] is envelope
    assert captured["attachments"] == [{"file_id": "file-1"}]
    assert captured["media_projection"] is media
    assert captured["input_identity"] == "turn-1"
    assert captured["thread_row"]["runtime_context_epoch"] == 2


@pytest.mark.asyncio
async def test_stale_resume_selects_fresh_cold_input_without_retry(monkeypatch) -> None:
    """A rejected handle cannot become an input failure or reuse old replay."""
    import infra.send_preflight as preflight
    from infra.destination_payloads import ExternalPreparedContextStale

    initial, current = _candidates(), _candidates()
    current.history_epoch = 1
    reads = iter((initial, current))
    calls = []

    async def in_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def stale(attempt, candidates, _decision, _exclude):
        assert candidates is initial
        calls.append("resume")
        raise ExternalPreparedContextStale("provider handle changed")

    async def exact(attempt, candidates, _exclude):
        assert candidates is current
        assert attempt.thread_row["runtime_context_epoch"] == 1
        assert attempt.current_user_content == "exact input"
        assert attempt.input_identity == "turn-1"
        calls.append("cold")
        return _Result("allowed", "exact_replay", 100), candidates

    monkeypatch.setattr(preflight, "run_in_db", in_db)
    monkeypatch.setattr(preflight, "_read_candidates", lambda *_args: next(reads))
    monkeypatch.setattr(preflight, "load_thread_row", lambda _thread: {
        "model": "claude-code/sonnet", "runtime_context_epoch": 1,
    })
    monkeypatch.setattr(preflight, "_provider_resume_attempt", stale)
    monkeypatch.setattr(preflight, "_render_reconstruction", exact)
    monkeypatch.setattr(
        preflight,
        "provider_resume_decision",
        lambda *_args, **_kwargs: SimpleNamespace(available=True, capacity=None),
    )

    result = await preflight.context_preflight(
        thread_id="thread-1", thread_row={"model": "claude-code/sonnet"},
        provider="cc", target=_target(), runtime_truth={"cc_has_session": True},
        current_user_content="exact input", preadmitted_turn_id="turn-1",
    )

    assert result["continuity_mode"] == "exact_replay"
    assert calls == ["resume", "cold"]


@pytest.mark.asyncio
async def test_provider_resume_uses_exact_render_as_missing_measurement_fallback(
    monkeypatch,
) -> None:
    """Missing telemetry preserves resume and uses one conservative render."""
    import infra.send_preflight as preflight

    calls: list[tuple] = []

    class Attempt:
        thread_id = "thread-1"
        provider = "cc"
        target = _target()

        async def cold(self, selection, *, trigger_reason):
            calls.append(("render", selection, trigger_reason))
            return _Result("allowed", "exact_replay", 95_000)

        async def live(
            self,
            candidates,
            *,
            retained_provider_tokens,
            canonical_input_tokens,
            trigger_reason,
        ):
            calls.append((
                "resume",
                retained_provider_tokens,
                canonical_input_tokens,
                trigger_reason,
            ))
            return _Result("allowed", "provider_resume", 95_000)

    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(preflight, "_read_candidates", lambda *_args: _candidates())
    monkeypatch.setattr(
        preflight,
        "provider_resume_decision",
        lambda *_args, **_kwargs: SimpleNamespace(
            available=True, capacity=None, retained_tokens=None,
        ),
    )

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": "claude-code/sonnet"},
        provider="cc",
        target=_target(),
        runtime_truth={"cc_has_session": True},
        current_user_content="continue",
    )

    assert result["continuity_mode"] == "provider_resume"
    assert calls == [
        ("render", "exact", None),
        ("resume", 0, 95_000, None),
    ]


@pytest.mark.asyncio
async def test_provider_resume_uses_the_shared_matching_capacity_decision(monkeypatch) -> None:
    """Send preflight receives the same trusted occupancy preview projects."""
    import infra.send_preflight as preflight

    calls = []

    class Attempt:
        async def cold(self, *_args, **_kwargs):
            pytest.fail("matching provider capacity must not reconstruct archive")

        async def live(self, _candidates, **kwargs):
            calls.append(kwargs)
            return _Result("allowed", "provider_resume", 12_050)

    decision = SimpleNamespace(
        available=True, capacity=object(), retained_tokens=12_030,
    )
    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(preflight, "_read_candidates", lambda *_args: _candidates())
    monkeypatch.setattr(
        preflight, "provider_resume_decision", lambda *_args, **_kwargs: decision,
    )

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": "claude-code/sonnet"},
        provider="cc",
        target=_target(),
        runtime_truth={"cc_has_session": True},
        current_user_content="continue",
    )

    assert result["continuity_mode"] == "provider_resume"
    assert calls == [{
        "retained_provider_tokens": 12_030,
        "canonical_input_tokens": None,
        "trigger_reason": None,
    }]


@pytest.mark.asyncio
async def test_blocked_resume_selects_fitting_installed_tail_without_fold(
    monkeypatch,
) -> None:
    """An installed generation wins immediately; an active fold is irrelevant."""
    import infra.send_preflight as preflight

    calls: list[tuple[str, str]] = []

    class Attempt:
        thread_id = "thread-1"
        provider = "cc"
        target = _target()

        async def cold(self, selection, *, trigger_reason):
            calls.append(("render", selection))
            if selection == "checkpoint-tail":
                return _Result("allowed", "checkpoint_tail", 20_000)
            return _Result("blocked", "exact_replay", 110_000)

        async def live(self, *_args, **_kwargs):
            calls.append(("resume", "live"))
            return _Result("blocked", "provider_resume", 110_000)

    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(
        preflight,
        "_read_candidates",
        lambda *_args: _candidates(checkpoint_tail="checkpoint-tail"),
    )
    monkeypatch.setattr(
        preflight,
        "provider_resume_decision",
        lambda *_args, **_kwargs: SimpleNamespace(
            available=True, capacity=None, retained_tokens=None,
        ),
    )

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": "claude-code/sonnet"},
        provider="cc",
        target=_target(),
        runtime_truth={"cc_has_session": True},
        current_user_content="continue",
    )

    assert result["continuity_mode"] == "checkpoint_tail"
    assert calls == [
        ("render", "checkpoint-tail"),
        ("resume", "live"),
        ("render", "checkpoint-tail"),
    ]


@pytest.mark.asyncio
async def test_cold_send_selects_checkpoint_tail_without_archive(
    monkeypatch,
) -> None:
    """Installed coverage replaces archive, regardless of archive fit."""
    import infra.send_preflight as preflight

    calls: list[str] = []

    class Attempt:
        async def cold(self, selection, *, trigger_reason):
            assert trigger_reason is None
            calls.append(selection)
            status = "allowed" if selection == "checkpoint-tail" else "blocked"
            mode = "checkpoint_tail" if status == "allowed" else "exact_replay"
            return _Result(status, mode, 20_000)

    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(
        preflight,
        "_read_candidates",
        lambda *_args: _candidates(checkpoint_tail="checkpoint-tail"),
    )

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": "claude-code/sonnet"},
        provider="cc",
        target=_target(),
        runtime_truth={},
        current_user_content="continue",
    )

    assert result["continuity_mode"] == "checkpoint_tail"
    assert calls == ["checkpoint-tail"]


@pytest.mark.asyncio
async def test_overfull_exact_input_is_returned_for_the_bound_turn_boundary(
    monkeypatch,
) -> None:
    """Preflight never waits before the exact Turn has a Stoppable live scope."""
    import infra.send_preflight as preflight

    blocked = _Result("blocked", "exact_replay", 110_000)

    class Attempt:
        async def cold(self, _selection, *, trigger_reason):
            assert trigger_reason is None
            return blocked

    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(preflight, "_read_candidates", lambda *_args: _candidates())

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": "claude-code/sonnet"},
        provider="cc",
        target=_target(),
        runtime_truth={},
        current_user_content="continue",
    )

    assert result["status"] == "blocked"
    assert result["prepared_context"] is blocked.prepared_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_truth",
    (
        {"cc_live": True, "cc_has_session": False, "busy": False},
        {"cc_live": False, "cc_has_session": False, "busy": False},
    ),
    ids=("settled-live-process", "restart"),
)
@pytest.mark.parametrize(
    "checkpoint_tail",
    ("model-switch-tail", "cross-harness-tail"),
    ids=("model-switch", "cross-harness"),
)
async def test_no_resume_uses_installed_tail_even_when_archive_would_fit(
    monkeypatch, runtime_truth, checkpoint_tail,
) -> None:
    """A fresh session reconstructs canonical coverage, even across harnesses."""
    import infra.send_preflight as preflight

    calls: list[str] = []

    class Attempt:
        async def cold(self, selection, *, trigger_reason):
            assert trigger_reason is None
            calls.append(selection)
            mode = "exact" if selection == "exact" else "checkpoint_tail"
            return _Result("allowed", mode, 20_000)

    monkeypatch.setattr(preflight, "_destination_attempt", lambda **_kwargs: Attempt())
    monkeypatch.setattr(
        preflight,
        "_read_candidates",
        lambda *_args: _candidates(checkpoint_tail=checkpoint_tail),
    )

    result = await preflight.context_preflight(
        thread_id="thread-1",
        thread_row={"model": "claude-code/sonnet"},
        provider="cc",
        target=_target(),
        runtime_truth=runtime_truth,
        current_user_content="continue",
    )

    assert result["continuity_mode"] == "checkpoint_tail"
    assert calls == [checkpoint_tail]


@pytest.mark.asyncio
async def test_claimed_batch_carries_overfull_input_to_the_live_boundary(
    monkeypatch,
) -> None:
    """The input remains claimed while the bound Turn performs its hard wait."""
    import infra.input_render as input_render
    import infra.runtime_truth as runtime_truth
    import infra.send_dispatch as send_dispatch
    import infra.send_preflight as send_preflight

    prepared = object()
    projection = SimpleNamespace(
        content="current input",
        source_content="current input",
        agent_envelope=None,
        attachments=(),
        media_projection=None,
    )

    async def truth(*_args, **_kwargs):
        return {"addressable": True, "execution_class": "cc_main"}

    async def load_projection(*_args, **_kwargs):
        return projection

    async def preflight(**_kwargs):
        return {
            "status": "blocked",
            "block_reason": "input exceeds window",
            "prepared_context": prepared,
        }

    monkeypatch.setattr(runtime_truth, "read_runtime_truth", truth)
    monkeypatch.setattr(input_render, "_load_input_projection_async", load_projection)
    monkeypatch.setattr(send_dispatch, "provider_for", lambda _klass: ("cc", False))
    monkeypatch.setattr(send_preflight, "context_preflight", preflight)

    result = await input_render.prepare_claimed_batch(
        SimpleNamespace(thread_id="thread-1", turn_id="turn-1"),
        thread_row={"id": "thread-1"},
        target=SimpleNamespace(harness="claude-code"),
    )

    assert result.prepared_context is prepared


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "allows_warm_checkpoint"),
    (("provider_resume", True), ("exact_replay", False)),
)
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_initial_boundary_uses_bound_turn_and_refreshes_on_adoption(
    monkeypatch,
    mode,
    allows_warm_checkpoint,
    harness,
) -> None:
    """The hard wait begins only after the harness exposes its exact Turn scope."""
    from core.checkpoint_fit import BoundaryDecision
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderContinuation,
    )
    from core.turn_core import TurnIdentity
    from subagents import harness_checkpoint_resume as checkpoint_resume

    identity = TurnIdentity("turn-1", 1)
    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=f"{harness}/test",
    )
    prepared = SimpleNamespace(
        mode=mode,
        fit_ledger=SimpleNamespace(
            rendered_input_tokens=110_000,
            effective_window_tokens=100_000,
            section_tokens=(("current_input", 500), ("tools", 100)),
        )
    )
    replay_selection = SimpleNamespace(checkpoint_generation_id="checkpoint-1")
    adopted = SimpleNamespace(
        mode="checkpoint_tail",
        replay=replay_selection,
        fit_ledger=SimpleNamespace(rendered_input_tokens=20_000),
    )
    context = SimpleNamespace(
        boundary=object(),
        target=SimpleNamespace(harness=harness, context_window=100_000),
        active_checkpoint=None,
        installed_checkpoint=None,
        registered_checkpoint_id=None,
        eager_checkpoint_id=None,
        binary=None,
        reasoning_effort=None,
        warm_boundary=SimpleNamespace(tool_call_id="completed-tool"),
    )
    prepared_calls: list[bool] = []
    capture_calls: list[object] = []
    capture_boundaries: list[object] = []
    tool_boundary_calls: list[tuple[object, object, object]] = []
    warm_source = object()
    refresh_calls = 0

    monkeypatch.setattr(
        checkpoint_resume,
        "read_external_checkpoint_context",
        lambda *_args, **_kwargs: context,
    )
    async def capture_tool_boundary(received_context, received_live, received_thread):
        tool_boundary_calls.append(
            (received_context, received_live, received_thread),
        )
        return warm_source

    monkeypatch.setattr(
        "core.checkpoint_codex_fork._capture_tool_boundary_warm_source",
        capture_tool_boundary,
    )

    async def capture(*args, **kwargs):
        capture_boundaries.append(args[1])
        capture_calls.append(kwargs.get("prefix_bytes"))
        return warm_source

    async def prepare(_checkpoint_id, **_kwargs):
        prepared_calls.append(_kwargs["warm_source"] is warm_source)
        return object()

    async def refresh(selection):
        nonlocal refresh_calls
        refresh_calls += 1
        assert selection is replay_selection
        return adopted

    async def maintain(boundary):
        assert boundary.turn == identity
        assert boundary.wait_scope is live.checkpoint_wait_scope
        assert boundary.canonical_input_tokens == 110_000
        await boundary.prepare_checkpoint("checkpoint-1", True)
        rendered = await boundary.render_installed(SimpleNamespace(id="checkpoint-1"))
        assert rendered == InstalledContinuation(20_000, adopted)
        return ProviderContinuation(
            BoundaryDecision.ADOPT_AND_PROCEED,
            identity,
            adopted,
            "checkpoint-1",
        )

    monkeypatch.setattr(
        "core.checkpoint_render.capture_warm_checkpoint_source", capture,
    )
    monkeypatch.setattr(checkpoint_resume, "_prepare_external_checkpoint", prepare)
    monkeypatch.setattr(
        "core.checkpoint_resume.installed_replay",
        lambda _record: replay_selection,
    )
    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)

    result = await checkpoint_resume.maintain_external_initial_boundary(
        live,
        "thread-1",
        harness,
        prepared,
        refresh,
    )

    assert result is adopted
    warm_at_initial_boundary = harness == "codex"
    assert prepared_calls == [warm_at_initial_boundary]
    assert tool_boundary_calls == (
        [(context, live, "thread-1")]
        if allows_warm_checkpoint and harness == "codex"
        else []
    )
    assert capture_boundaries == (
        [context.boundary]
        if mode != "provider_resume" and harness == "codex"
        else []
    )
    assert capture_calls == ([None] if capture_boundaries else [])
    assert refresh_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
@pytest.mark.parametrize("checkpoint_state", ("boundary", "winner", "no_boundary"))
async def test_external_initial_boundary_uses_current_checkpoint_state(
    monkeypatch,
    harness,
    checkpoint_state,
) -> None:
    from core.checkpoint_fit import BoundaryDecision
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from core.turn_core import TurnIdentity
    from subagents import harness_checkpoint_resume as checkpoint_resume

    identity = TurnIdentity("turn-current", 2)
    target = SimpleNamespace(harness=harness, context_window=100_000)
    winner = SimpleNamespace(id="installed-winner")
    context = SimpleNamespace(
        boundary=object() if checkpoint_state == "boundary" else None,
        previous_generation_id=winner.id if checkpoint_state == "winner" else None,
        target=target,
        active_checkpoint=None,
        installed_checkpoint=winner if checkpoint_state == "winner" else None,
        registered_checkpoint_id=None,
        eager_checkpoint_id=winner.id if checkpoint_state == "winner" else None,
    )
    reads = []

    def read_context(*args, **kwargs):
        reads.append((args, kwargs))
        return context

    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=f"{harness}/test",
    )
    prepared = SimpleNamespace(
        mode="exact_replay",
        fit_ledger=SimpleNamespace(
            rendered_input_tokens=110_000,
            effective_window_tokens=100_000,
            section_tokens=(("current_input", 500),),
        ),
    )
    captured = {}
    monkeypatch.setattr(
        checkpoint_resume, "read_external_checkpoint_context", read_context,
    )

    async def prepare(_checkpoint_id, _required, **kwargs):
        captured["context"] = kwargs["context"]
        return object()

    async def maintain(boundary):
        captured["installed"] = boundary.installed_checkpoint
        captured["prepare_available"] = boundary.prepare_checkpoint is not None
        if boundary.prepare_checkpoint is not None:
            await boundary.prepare_checkpoint("checkpoint-next", True)
        return ProviderContinuation(BoundaryDecision.PROCEED, identity)

    monkeypatch.setattr(
        checkpoint_resume, "_prepare_initial_external_checkpoint", prepare,
    )
    monkeypatch.setattr(
        "core.checkpoint_resume.maintain_before_provider_call", maintain,
    )

    assert await checkpoint_resume.maintain_external_initial_boundary(
        live,
        "thread-1",
        harness,
        prepared,
        lambda _selection: None,
    ) is None
    expected_reads = 2 if checkpoint_state == "boundary" else 1
    assert len(reads) == expected_reads
    assert all(
        kwargs == {"current_turn_id": identity.turn_id, "exact_history": False}
        for _args, kwargs in reads
    )
    assert captured == {
        "installed": winner if checkpoint_state == "winner" else None,
        "prepare_available": checkpoint_state == "boundary",
        **({"context": context} if checkpoint_state == "boundary" else {}),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_initial_boundary_adopts_current_installed_winner(
    monkeypatch,
    harness,
) -> None:
    from core.checkpoint_resume import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import harness_checkpoint_resume as checkpoint_resume

    identity = TurnIdentity("turn-current", 2)
    winner = SimpleNamespace(id="installed-winner")
    context = SimpleNamespace(
        boundary=None,
        previous_generation_id=winner.id,
        target=SimpleNamespace(harness=harness, context_window=100_000),
        active_checkpoint=None,
        installed_checkpoint=winner,
        registered_checkpoint_id=None,
        eager_checkpoint_id=winner.id,
    )
    monkeypatch.setattr(
        checkpoint_resume,
        "read_external_checkpoint_context",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        "core.checkpoint_resume.installed_replay",
        lambda record: SimpleNamespace(checkpoint_generation_id=record.id),
    )

    async def reject_prepare(*_args, **_kwargs):
        raise AssertionError("an installed winner must bypass checkpoint rendering")

    async def refresh(selection):
        assert selection.checkpoint_generation_id == winner.id
        return SimpleNamespace(
            mode="checkpoint_tail",
            replay=SimpleNamespace(checkpoint_generation_id=winner.id),
            fit_ledger=SimpleNamespace(rendered_input_tokens=12_000),
        )

    monkeypatch.setattr(
        checkpoint_resume, "_prepare_initial_external_checkpoint", reject_prepare,
    )
    live = SimpleNamespace(
        corsia_turn=identity,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=f"{harness}/test",
    )
    prepared = SimpleNamespace(
        mode="exact_replay",
        fit_ledger=SimpleNamespace(
            rendered_input_tokens=110_000,
            effective_window_tokens=100_000,
            section_tokens=(("current_input", 500),),
        ),
    )

    continuation = await checkpoint_resume.maintain_external_initial_boundary(
        live,
        "thread-1",
        harness,
        prepared,
        refresh,
    )
    assert continuation.mode == "checkpoint_tail"
    assert continuation.replay.checkpoint_generation_id == winner.id


@pytest.mark.asyncio
async def test_external_initial_boundary_rejects_wrong_checkpoint_generation(
    monkeypatch,
) -> None:
    """A selection callback cannot substitute a different checkpoint replay."""
    from core.checkpoint_resume import CheckpointContinuationError
    from subagents import harness_checkpoint_resume as checkpoint_resume

    selection = SimpleNamespace(checkpoint_generation_id="checkpoint-1")
    wrong = SimpleNamespace(
        mode="checkpoint_tail",
        replay=SimpleNamespace(checkpoint_generation_id="checkpoint-2"),
        fit_ledger=SimpleNamespace(rendered_input_tokens=20_000),
    )

    async def refresh(received):
        assert received is selection
        return wrong

    monkeypatch.setattr(
        "core.checkpoint_resume.installed_replay",
        lambda _record: selection,
    )
    with pytest.raises(CheckpointContinuationError, match="installed generation"):
        await checkpoint_resume._render_initial_installed(
            SimpleNamespace(id="checkpoint-1"),
            refresh_prepared=refresh,
        )


@pytest.mark.asyncio
async def test_cc_acceptance_runs_boundary_after_turn_bind_before_provider_write(
    monkeypatch,
) -> None:
    """Checkpoint adoption may swap the handle before the current input is sent."""
    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import cc_accept

    identity = TurnIdentity("turn-1", 1)
    wait_scope = CheckpointWaitScope()
    original = SimpleNamespace(checkpoint_wait_scope=wait_scope)
    replacement = SimpleNamespace(checkpoint_wait_scope=wait_scope)
    original_frame = SimpleNamespace(turn_text="original")
    replacement_frame = SimpleNamespace(turn_text="replacement")
    order: list[str] = []

    async def admit(*_args, **_kwargs):
        order.append("bind")
        return identity

    async def boundary(live, turn):
        assert live is original
        assert turn == identity
        order.append("boundary")
        return replacement, replacement_frame

    async def feed(_thread_id, live, frame, _observation, **_kwargs):
        assert live is replacement
        assert frame is replacement_frame
        order.append("write")

    monkeypatch.setattr(cc_accept, "_admit_turn", admit)
    monkeypatch.setattr(cc_accept, "_prepare_live_turn", lambda *_args: None)
    monkeypatch.setattr(cc_accept, "_feed_frame", feed)

    result = await cc_accept.accept_reconstructed_input(
        "thread-1",
        original,
        original_frame,
        turn_id=identity.turn_id,
        bcast=lambda *_args: None,
        before_provider_write=boundary,
    )

    assert result == identity
    assert order == ["bind", "boundary", "write"]


@pytest.mark.asyncio
async def test_codex_initial_boundary_replaces_bound_attempt_before_turn_start(
    monkeypatch,
) -> None:
    """Codex sends the current input only from the adopted checkpoint attempt."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents import codex_turn_transitions
    from subagents import harness_checkpoint_resume

    identity = TurnIdentity("turn-1", 1)
    prepared = object()
    adopted = object()
    live = SimpleNamespace(corsia_turn=identity)
    calls: list[str] = []

    async def refresh():
        return adopted

    spec = SimpleNamespace(
        prepared_context=prepared,
        refresh_prepared_context=refresh,
        thread_id="thread-1",
        text="rendered provider payload",
        current_user_text="current input",
        catalog_model_id="codex/luna",
        mark_accepted=None,
        mark_ambiguous=None,
        settle_delivery=None,
        media_projection=None,
        held_lock=asyncio.Lock(),
    )

    async def maintain(current, *_args):
        assert current.corsia_turn == identity
        calls.append("boundary")
        return adopted

    async def resume(_thread_id, current, selected, **kwargs):
        assert current.corsia_turn == identity
        assert selected is adopted
        assert kwargs["current_user_text"] == "current input"
        calls.append("write")
        return identity

    monkeypatch.setattr(
        harness_checkpoint_resume, "maintain_external_initial_boundary", maintain
    )
    monkeypatch.setattr(
        codex_turn_transitions, "resume_same_turn_after_checkpoint_locked", resume
    )
    monkeypatch.setattr(codex_turn_transitions, "_require_provider_fit", lambda *_a: None)

    result = await codex_runtime._maintain_codex_initial_boundary(spec, live)
    assert result == identity
    assert calls == ["boundary", "write"]
