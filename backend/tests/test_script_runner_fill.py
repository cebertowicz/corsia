"""A free run slot is filled when it frees, whichever script the oldest queued runs are for.

Through the real intake, evaluation, script runner, settlement and continuation loop; only
the sandboxed process is faked.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from automation import script_runner, script_schedules
from automation.continuation import loop
from events import rule_runs
from tests.test_event_rules_run_script import (  # noqa: F401 - the fixtures and their helpers
    _emit, _outcomes, _rule, _script, runs, world,
)


def _drive_once() -> int:
    """One sweep's script phase, with the runs it started settled; how many it started."""

    async def go():
        started = await script_runner.drive(datetime.now(timezone.utc).isoformat())
        await asyncio.gather(*script_runner._in_flight.values())
        return started

    return asyncio.run(go())


def _two_scripts(runs) -> tuple[dict, dict]:
    """a.sh runs on ``qa.route`` and b.sh on ``qa.two``, so a test sets the queue's order."""
    return _rule(_script(runs, "a.sh")), _rule(_script(runs, "b.sh"), "two")


def _read_limits(monkeypatch) -> list[int]:
    limits: list[int] = []
    real = rule_runs.queued

    def spy(db, limit):
        limits.append(limit)
        return real(db, limit)

    monkeypatch.setattr(rule_runs, "queued", spy)
    return limits


def _codes(runs) -> list[str]:
    return [event["facts"]["code"] for _name, event in runs["calls"]]


def test_two_queued_runs_of_one_script_do_not_leave_the_other_slot_empty(runs, monkeypatch):
    first, _second = _two_scripts(runs)
    for code in ("a1", "a2", "a3"):
        _emit(code=code)
    for code in ("b1", "b2"):
        _emit("qa.two", code)  # behind three runs of a.sh
    limits = _read_limits(monkeypatch)

    assert _drive_once() == script_runner.SCRIPT_RUN_CAP == 2
    passes = [set(_codes(runs))]  # runs one pass starts run together, so they call in no set order
    assert passes == [{"a1", "b1"}]  # a2 waits for a1; the slot it can't take goes to b.sh
    assert limits == [2, 1]  # the second read asks only for the slot still free
    assert [outcome for outcome, _reason in _outcomes(first["id"])] == ["ran", "queued", "queued"]

    while _drive_once():
        passes.append(set(_codes(runs)) - set().union(*passes))
    assert passes == [{"a1", "b1"}, {"a2", "b2"}, {"a3"}]  # each script's own runs, oldest first


def test_a_script_that_cannot_be_claimed_ends_the_pass(runs, monkeypatch):
    _two_scripts(runs)
    _emit(code="a1")
    _emit("qa.two", "b1")
    monkeypatch.setattr(script_schedules, "claim_for_rule", lambda *_args, **_kwargs: False)
    limits = _read_limits(monkeypatch)

    assert _drive_once() == 0
    assert limits == [2, 2]  # one read per slot at most, never a loop on the same rows
    assert runs["calls"] == []


def test_a_settled_run_starts_the_next_queued_one_without_waiting_for_the_sweep_interval(runs, monkeypatch):
    rules = [_rule(_script(runs, f"many-{i}.sh")) for i in range(3)]
    _emit()  # one event, three rules: three runs queued for two slots
    monkeypatch.setattr(loop.restart_jitter, "RESTART_JITTER_SECONDS", 0)  # the 60-second interval from the start

    async def go():
        task = asyncio.create_task(loop.continuation_sweep_loop())
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(10):
                    while len(runs["calls"]) < 3 or script_runner._in_flight:
                        await asyncio.sleep(0.05)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert [_outcomes(rule["id"]) for rule in rules] == [[("ran", None)]] * 3
