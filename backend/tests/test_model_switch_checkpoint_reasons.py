"""A model switch that falls back to a checkpoint records why."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from loguru import logger

from core.fork_fit import soft_pressure_remaining
from thread import model_prepare
from thread.model_state import ModelSwitchCheckpointRequired

WINDOW = 262_000


def _context(**overrides):
    values = dict(
        thread_id="thread-1",
        user_id=None,
        generation=1,
        source_model="sonnet",
        source_selected_model="sonnet",
        source_target=SimpleNamespace(harness="claude_code"),
        source_harness="claude_code",
        destination_model="opus",
        destination_target=SimpleNamespace(harness="claude_code", context_window=WINDOW),
        project_id=None,
        source_runtime_context_epoch=1,
        destructive_version=(1, 1, 1),
        source_active_turn_id=None,
    )
    values.update(overrides)
    return model_prepare._PreparationContext(**values)


class _Harness:
    def __init__(self, monkeypatch, *, context=None, candidates=None, usage=None):
        self.live_fit_calls = 0
        self.render_calls = 0
        self.rendered_tokens = 10
        self.rendered_effective_window = WINDOW
        self.owner = object()
        self.live_status = "allowed"
        self.context = context or _context()
        self.candidates = candidates or SimpleNamespace(
            checkpoint_tail=None,
            exact=SimpleNamespace(mode="exact", history_epoch=1, sealed_boundary=None),
            last_resort=None,
            history_epoch=1,
            sealed_boundary=None,
        )
        self.usage = usage
        fit_input = SimpleNamespace(receipts=[SimpleNamespace(intent="apply_now")])

        async def read_context(*_a, **_k):
            return self.context

        async def read_candidates(*_a, **_k):
            return self.candidates, "tv", fit_input, None, self.context.destination_target

        async def run_in_db(fn, *a, **k):
            return await read_candidates()

        async def usage_fn(*_a, **_k):
            return self.usage

        async def owner_fn(*_a, **_k):
            return self.owner

        def live_fit(**_k):
            self.live_fit_calls += 1
            ledger = SimpleNamespace(
                rendered_input_tokens=self.usage + 9, effective_window_tokens=WINDOW - 32_000
            )
            return SimpleNamespace(
                status=self.live_status,
                prepared_context=SimpleNamespace(fit_ledger=ledger),
            )

        async def render(**_k):
            self.render_calls += 1
            rendered = SimpleNamespace(input_tokens=self.rendered_tokens)
            ledger = SimpleNamespace(
                effective_window_tokens=self.rendered_effective_window
            )
            return SimpleNamespace(
                prepared_context=SimpleNamespace(rendered=rendered, fit_ledger=ledger)
            )

        monkeypatch.setattr(model_prepare, "_read_preparation_context", read_context)
        monkeypatch.setattr(model_prepare, "_live_same_harness_context_usage", usage_fn)
        monkeypatch.setattr(model_prepare, "_capture_model_switch_source_owner", owner_fn)
        monkeypatch.setattr(model_prepare, "_live_model_switch_fit", live_fit)
        monkeypatch.setattr(model_prepare, "_render_model_switch_projection", render)
        monkeypatch.setattr(model_prepare, "_projection_proof", lambda _s: None)
        monkeypatch.setattr(model_prepare, "_read_model_switch_candidates", object())
        monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)


@pytest.fixture
def log_lines():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    yield lines
    logger.remove(sink)


async def _prepare():
    return await model_prepare.prepare_model_switch("thread-1", "opus", user_id=None)


@pytest.mark.asyncio
async def test_cross_harness_over_window_records_both_reasons(monkeypatch, log_lines):
    harness = _Harness(monkeypatch, usage=None)
    harness.rendered_tokens = WINDOW + 1

    with pytest.raises(ModelSwitchCheckpointRequired) as caught:
        await _prepare()

    launch_point = WINDOW - soft_pressure_remaining(WINDOW)
    assert caught.value.reasons == (
        "no_live_usage+replay_over_launch_point("
        f"mode=exact input={WINDOW + 1} launch_point={launch_point} "
        f"effective_window={WINDOW})",
    )
    fallbacks = [line for line in log_lines if "needs checkpoint" in line]
    assert len(fallbacks) == 1
    assert "source=sonnet destination=opus lengths=1" in fallbacks[0]
    assert "no_live_usage" in fallbacks[0]


@pytest.mark.asyncio
async def test_replay_above_effective_window_launch_point_requires_checkpoint(
    monkeypatch, log_lines
):
    harness = _Harness(monkeypatch, usage=None)
    harness.rendered_effective_window = 200_000
    harness.rendered_tokens = 230_000

    with pytest.raises(ModelSwitchCheckpointRequired) as caught:
        await _prepare()

    launch_point = 200_000 - soft_pressure_remaining(200_000)
    assert caught.value.reasons == (
        "no_live_usage+replay_over_launch_point("
        f"mode=exact input=230000 launch_point={launch_point} "
        "effective_window=200000)",
    )


@pytest.mark.asyncio
async def test_replay_just_under_effective_window_launch_point_commits(
    monkeypatch, log_lines
):
    harness = _Harness(monkeypatch, usage=None)
    harness.rendered_effective_window = 200_000
    harness.rendered_tokens = 200_000 - soft_pressure_remaining(200_000) - 1

    prepared = await _prepare()

    assert prepared is not None
    assert prepared.destination_fit_ledger.effective_window_tokens == 200_000
    assert not [line for line in log_lines if "needs checkpoint" in line]


@pytest.mark.asyncio
async def test_replay_at_effective_window_launch_point_requires_checkpoint(
    monkeypatch, log_lines
):
    # Pressure launches at this occupancy (fork_fit.evaluate_boundary),
    # so a switch must not land a thread exactly there.
    harness = _Harness(monkeypatch, usage=None)
    harness.rendered_effective_window = 200_000
    launch_point = 200_000 - soft_pressure_remaining(200_000)
    harness.rendered_tokens = launch_point

    with pytest.raises(ModelSwitchCheckpointRequired) as caught:
        await _prepare()

    assert caught.value.reasons == (
        "no_live_usage+replay_over_launch_point("
        f"mode=exact input={launch_point} launch_point={launch_point} "
        "effective_window=200000)",
    )


@pytest.mark.asyncio
async def test_no_live_owner_with_fitting_replay_returns_replay_and_logs_nothing(
    monkeypatch, log_lines
):
    harness = _Harness(monkeypatch, usage=100_000)
    harness.owner = None

    prepared = await _prepare()

    assert prepared is not None
    assert prepared.projection_mode == "exact"
    assert harness.render_calls == 1
    assert not [line for line in log_lines if "needs checkpoint" in line]


@pytest.mark.asyncio
async def test_live_fit_blocked_is_recorded_and_skips_replay(monkeypatch, log_lines):
    harness = _Harness(monkeypatch, usage=172_000)
    harness.live_status = "blocked"

    with pytest.raises(ModelSwitchCheckpointRequired) as caught:
        await _prepare()

    assert caught.value.reasons == (
        f"live_fit_blocked(status=blocked used=172000 input=172009"
        f" effective_window={WINDOW - 32_000} window={WINDOW})",
    )
    assert harness.live_fit_calls == 1
    assert harness.render_calls == 0


@pytest.mark.asyncio
async def test_no_candidates_records_stub_count(monkeypatch, log_lines):
    candidates = SimpleNamespace(
        checkpoint_tail=None,
        exact=None,
        last_resort=SimpleNamespace(tail=SimpleNamespace(retention_stub_seqs=(3, 5))),
        history_epoch=1,
        sealed_boundary=None,
    )
    harness = _Harness(monkeypatch, usage=None, candidates=candidates)

    with pytest.raises(ModelSwitchCheckpointRequired) as caught:
        await _prepare()

    assert caught.value.reasons == ("no_live_usage+no_replay_candidate(stubs=2)",)
    assert harness.render_calls == 0


@pytest.mark.asyncio
async def test_live_not_allowed_is_recorded(monkeypatch, log_lines):
    harness = _Harness(monkeypatch, usage=1)
    harness.rendered_tokens = WINDOW + 5

    with pytest.raises(ModelSwitchCheckpointRequired) as caught:
        await model_prepare.prepare_model_switch(
            "thread-1", "opus", user_id=None, allow_live_same_harness=False
        )

    assert caught.value.reasons[0].startswith(
        "live_not_allowed+replay_over_launch_point"
    )
