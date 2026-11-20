"""A fold held by a usage limit with no readable reset time is still released.

Such a limit is never due, so the release sweep starts the account's own usage
read. Composed through the real fold, Turn terminal, sweep, usage read, verdict
write and scope release; only the Claude Code processes and the Turn starter
are substituted.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from connections.usage_windows import parse_time
from core.checkpoint_resume import InstalledContinuation, _required_checkpoint_continuation
from core.checkpoint_types import CheckpointUnavailable
from infra.database import get_db
from test_checkpoint_limit_composed import (
    THREAD, _boundary, _finish, _seed, _succeed, _window,
)
from test_checkpoint_limit_frames import FIVE_HOUR_REFUSAL, limit_frame, use_cc_frame
from test_provider_sources import _ok, _stream

pytestmark = pytest.mark.usefixtures("signed_in_defaults")
DISCOVERED_MODELS = {"claude-code": ("catalog-claude-code",)}
#: Real refusal wording whose reset time cannot be read.
NO_RESET_REFUSAL = "You've hit your 5-hour limit · resets soon"
MODEL = "catalog-claude-code"


@pytest.fixture(autouse=True)
async def _runtime(monkeypatch):
    """The app loop the release is handed to, and a clean usage-read throttle."""
    import infra.app_loop as app_loop
    from connections import harness_usage_check as checks
    from core import checkpoint_runtime, fork_runtime

    async def no_wake(_record):
        return None

    monkeypatch.setattr(checkpoint_runtime, "_notify_completion", no_wake)
    checks._LAST_STARTED.clear()
    app_loop.capture(asyncio.get_running_loop())
    yield
    app_loop.clear()
    for task in tuple(checks._CHECKS.values()):
        task.cancel()
    checks._LAST_STARTED.clear()
    await fork_runtime.shutdown_forks()
    await checkpoint_runtime.shutdown_checkpoint_wakes()


async def _held_on_limit(monkeypatch, refusal: str = NO_RESET_REFUSAL):
    from infra.held_admission import limited_admission

    prior = await asyncio.to_thread(_seed, "claude-code")
    use_cc_frame(monkeypatch, limit_frame("error_only", None, refusal))
    with pytest.raises(CheckpointUnavailable) as raised:
        await _required_checkpoint_continuation(_boundary("claude-code", prior, _window("claude-code"), []),
                                                None, prior)
    assert raised.value.limited_target == MODEL
    _terminal, state = await asyncio.to_thread(_finish, raised.value)
    assert state == ("queued", None)
    held = limited_admission(THREAD, MODEL)
    assert held is not None
    return prior, held


def _usage_read(monkeypatch, *, utilization: int) -> list:
    """Substitute the usage read's Claude Code control process; return its calls."""
    from connections import claude_discovery, harness_usage_check as checks

    calls = []

    async def control(_binary, frames, **_kwargs):
        calls.append([frame["request_id"] for frame in frames])
        return _stream(_ok("corsia-usage", {
            "rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": utilization}},
        }))

    monkeypatch.setattr(claude_discovery, "run_claude_control", control)
    monkeypatch.setattr(checks, "_binary_for", lambda _provider: asyncio.sleep(0, result="/synthetic/claude"))
    return calls


def _released_turns(monkeypatch) -> AsyncMock:
    started = AsyncMock(return_value=None)
    monkeypatch.setattr("infra.thread_execution.drive_idle_thread", started)
    return started


async def _sweep() -> None:
    """One sweep's release phase, then let the usage read and any release it hands off finish."""
    from connections import harness_usage_check as checks
    from infra.connection_limit_drive import drive_limit_releases

    from ws.ingress import background_tasks

    await drive_limit_releases(datetime.now(timezone.utc))
    await asyncio.gather(*checks._CHECKS.values(), return_exceptions=True)
    for _ in range(5):  # the release is handed to the app loop, then reads the DB off-loop
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(background_tasks), return_exceptions=True)


def _input_state() -> str:
    db = get_db()
    try:
        return db.execute("SELECT delivery_state FROM messages WHERE id=?", (f"{THREAD}-input",)).fetchone()[0]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sweep_reads_usage_for_a_no_reset_limit_and_an_allowed_read_releases_the_fold_first(monkeypatch):
    from core.checkpoint_resume import BoundaryDecision
    from infra import checkpoint_queries
    from infra.held_admission import limited_admission

    prior, held = await _held_on_limit(monkeypatch)
    assert held.resets_at is None  # nothing would ever make this due
    calls = _usage_read(monkeypatch, utilization=1)
    started = _released_turns(monkeypatch)

    await _sweep()

    assert calls == [["corsia-usage", "corsia-usage-end"]]
    assert limited_admission(THREAD, MODEL) is None
    assert [call.args[0] for call in started.await_args_list] == [THREAD]  # the held head is driven
    # That Turn's first boundary folds before any provider call.
    _succeed(monkeypatch, "claude-code")
    window = _window("claude-code")
    prepared: list[str] = []
    boundary = replace(_boundary("claude-code", prior, window, prepared), render_installed=AsyncMock(
        side_effect=lambda record: InstalledContinuation(window + 1 if record.id == "prior" else 1, None)))
    continuation = await _required_checkpoint_continuation(boundary, None, prior)
    assert continuation.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert checkpoint_queries.current_installed(THREAD).raw_result == "Fresh narrative\n"


@pytest.mark.asyncio
async def test_a_still_limited_read_releases_nothing_and_a_second_sweep_inside_the_throttle_reads_nothing(
    monkeypatch,
):
    from infra.held_admission import limited_admission

    _prior, held = await _held_on_limit(monkeypatch)
    assert held.resets_at is None
    calls = _usage_read(monkeypatch, utilization=100)
    started = _released_turns(monkeypatch)

    await _sweep()
    await _sweep()

    assert calls == [["corsia-usage", "corsia-usage-end"]]  # one read per five minutes per login
    assert limited_admission(THREAD, MODEL) is not None
    assert started.await_count == 0
    assert _input_state() == "queued"  # still held, never failed


@pytest.mark.asyncio
async def test_a_limit_with_a_readable_reset_waits_for_it_and_starts_no_usage_read(monkeypatch):
    from infra.held_admission import limited_admission

    _prior, held = await _held_on_limit(monkeypatch, FIVE_HOUR_REFUSAL)
    reset = parse_time(held.resets_at)
    assert reset is not None and reset > datetime.now(timezone.utc)
    calls = _usage_read(monkeypatch, utilization=1)
    started = _released_turns(monkeypatch)

    await _sweep()

    assert calls == []  # its own reset releases it; the probe is only for limits with none
    assert limited_admission(THREAD, MODEL) is not None
    assert started.await_count == 0
