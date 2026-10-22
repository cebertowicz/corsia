"""R23: a rule-started worker at a full Claude Code limit is held, never lost (design §7.4 a-d).

The limit is set to one, and a real worker that never answers fills it. Killing that worker
frees the slot for real; the runner, idle claim and capacity check decide the rest.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, atool, make_rule, owner_wakes, request, starts, world,
)

CAP = 1


@pytest.fixture
def full(world, monkeypatch):
    monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live", lambda: CAP)
    world["reply"].write_text("HANG")
    return world


def _working() -> int:
    from infra.database import get_db
    from infra.worker_capacity import working

    return working(get_db())


def _turns(worker: str) -> list[tuple]:
    from infra.database import get_db

    return [tuple(row) for row in get_db().execute(
        "SELECT status, error FROM active_turns WHERE thread_id=? ORDER BY turn_order", (worker,))]


def _held_inputs(worker: str) -> int:
    """The worker's held task (its held batch also carries a context digest)."""
    from infra.database import get_db

    return get_db().execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='user' AND delivery_state='queued' "
        "AND write_outcome='proved_no_write' AND delivery_error='worker_capacity'", (worker,)).fetchone()[0]


async def _fill(rule: str) -> str:
    """Start the worker that fills the one slot; its id."""
    from automation import worker_runner
    from subagents.cc_runtime_state import _LIVE

    await request("fill")
    assert await worker_runner.drive() == 1
    worker = starts(rule)[0][3]
    for _ in range(200):
        if (live := _LIVE.get(worker)) is not None and live.session._proc is not None:
            return worker
        await asyncio.sleep(0.02)
    raise AssertionError("the filling worker never started")


async def _kill(worker: str) -> None:
    from subagents.cc_runtime_state import _LIVE

    _LIVE.get(worker).session._proc.kill()
    for _ in range(200):
        if _turns(worker)[-1][0] != "running":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the killed worker's turn never ended")


async def _passes(n: int = 5) -> None:
    from automation import worker_runner

    for _ in range(n):
        await worker_runner.drive()
        assert _working() <= CAP
        await asyncio.sleep(0.02)


# (a) cap full, rule fires: one thread, one held input, one Turn-end record, no process.
@pytest.mark.asyncio
async def test_a_start_at_a_full_limit_makes_one_held_worker_and_no_process(full):
    from automation import worker_runner
    from subagents.cc_runtime_state import _LIVE

    rule = await make_rule()
    await _fill(rule)
    event = await request()
    assert await worker_runner.drive() == 1
    [(_fill_row), (outcome, reason, identity, worker)] = starts(rule)
    assert (outcome, reason, identity) == ("held", "worker capacity", "pr-1@a1")
    assert _held_inputs(worker) == 1
    assert _turns(worker) == [("error", "worker_capacity")]
    assert _LIVE.get(worker) is None and _working() == CAP
    explained = await atool(OWNER, operation="explain", event_id=event)
    assert [d["explanation"] for d in explained["decisions"]] == [
        f"rule {rule} is holding {OWNER}'s worker start: worker capacity"]
    assert owner_wakes() == []


# (b) two unrelated catalog changes: no new Turn rows, nothing released.
@pytest.mark.asyncio
async def test_catalog_changes_neither_release_nor_retry_a_held_start(full):
    from automation import worker_runner
    from infra.thread_execution import drive_idle_thread
    from infra.unavailable_model_recovery import _resume_held_heads

    rule = await make_rule()
    await _fill(rule)
    await request()
    await worker_runner.drive()
    worker = starts(rule)[1][3]
    for _ in range(2):
        await _resume_held_heads()
        await drive_idle_thread(worker)  # even offered directly, the claim finds no slot
    assert _turns(worker) == [("error", "worker_capacity")] and _held_inputs(worker) == 1
    assert starts(rule)[1][0] == "held" and owner_wakes() == []


# (c) one slot frees with three held: exactly one starts, the oldest, and the limit holds.
@pytest.mark.asyncio
async def test_a_freed_slot_starts_exactly_the_oldest_held_worker(full):
    from automation import worker_runner

    rule = await make_rule()
    filler = await _fill(rule)
    for name in ("first", "second", "third"):
        await request(name)
    assert await worker_runner.drive() == 3
    held = [row[3] for row in starts(rule)[1:]]
    assert [row[0] for row in starts(rule)[1:]] == ["held"] * 3

    await _kill(filler)
    for _ in range(100):
        await _passes(1)
        if starts(rule)[1][0] == "started" and len(_turns(held[0])) == 2:
            break
    assert [row[0] for row in starts(rule)] == ["no_result", "started", "held", "held"]
    assert _turns(held[0]) == [("error", "worker_capacity"), ("running", None)]
    assert [_held_inputs(worker) for worker in held] == [0, 1, 1]
    await _passes()
    assert [row[0] for row in starts(rule)][1:] == ["started", "held", "held"]


# (d) owner paused while held, slot frees: nothing starts.
@pytest.mark.asyncio
async def test_a_paused_owner_keeps_its_held_start_held_when_a_slot_frees(full, monkeypatch):
    from automation import worker_runner

    rule = await make_rule()
    filler = await _fill(rule)
    await request()
    await worker_runner.drive()
    worker = starts(rule)[1][3]
    monkeypatch.setattr("plans.public.thread_paused_since",
                        lambda _db, thread: "2026-01-01" if thread == OWNER else None)
    await _kill(filler)
    await _passes()
    assert starts(rule)[1][0] == "held" and _held_inputs(worker) == 1
    assert _turns(worker) == [("error", "worker_capacity")] and _working() == 0
