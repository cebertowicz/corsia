"""R23: at a full Claude Code limit a rule's start waits in the queue, never lost (design §7.4).

The limit is set to one, and a real worker that never answers fills it. Killing that worker
frees the slot for real; the runner's claim and spawn's own limit check decide the rest.
No worker, thread or input exists for a start until it is claimed with a slot free.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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
    from subagents.cc_idle_retirement import working_thread_ids

    return len(working_thread_ids())


def _stored() -> tuple:
    """Every start row and the thread, input and Turn counts: what a pass could write."""
    from infra.database import get_db

    db = get_db()
    return (
        [tuple(row) for row in db.execute("SELECT * FROM event_route_decisions ORDER BY id")],
        *(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
          for table in ("threads", "messages", "active_turns")),
    )


async def _until_working(worker: str) -> str:
    from subagents.cc_runtime_state import _LIVE

    for _ in range(200):
        if (live := _LIVE.get(worker)) is not None and live.session._proc is not None:
            return worker
        await asyncio.sleep(0.02)
    raise AssertionError(f"{worker} never started")


async def _fill(rule: str) -> str:
    """Start the worker that fills the one slot; its id."""
    from automation import worker_runner

    await request("fill")
    assert await worker_runner.drive() == 1
    return await _until_working(starts(rule)[0][3])


async def _kill(worker: str) -> None:
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    _LIVE.get(worker).session._proc.kill()
    for _ in range(200):
        if get_db().execute("SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'",
                            (worker,)).fetchone() is None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the killed worker's turn never ended")


async def _passes(n: int = 5) -> None:
    from automation import worker_runner

    for _ in range(n):
        await worker_runner.drive()
        assert _working() <= CAP
        await asyncio.sleep(0.02)


# (a) cap full, rule fires: the start stays queued; no thread, no spawn, no write in a pass.
@pytest.mark.asyncio
async def test_a_start_at_a_full_limit_stays_queued_and_a_pass_writes_nothing(full):
    from automation import worker_runner

    rule = await make_rule()
    await _fill(rule)
    event = await request()
    before = _stored()
    assert await worker_runner.drive() == 0 and await worker_runner.drive() == 0
    assert _stored() == before
    assert starts(rule)[1] == ("queued", None, "pr-1@a1", None) and _working() == CAP
    explained = await atool(OWNER, operation="explain", event_id=event)
    assert [d["explanation"] for d in explained["decisions"]] == [
        f"rule {rule} will start a worker for {OWNER} with it"]
    assert owner_wakes() == []


# (b) two unrelated catalog changes: no new Turn rows, and the start is still queued.
@pytest.mark.asyncio
async def test_catalog_changes_make_no_turn_for_a_queued_start(full):
    from automation import worker_runner
    from infra.unavailable_model_recovery import _resume_held_heads

    rule = await make_rule()
    await _fill(rule)
    await request()
    await worker_runner.drive()
    before = _stored()
    for _ in range(2):
        await _resume_held_heads()
    assert _stored()[3] == before[3] and starts(rule)[1][0] == "queued" and owner_wakes() == []


# (c) three queued at a full cap, one slot frees: exactly the oldest is claimed, and the limit holds.
@pytest.mark.asyncio
async def test_a_freed_slot_claims_exactly_the_oldest_queued_start(full):
    from automation import worker_runner

    rule = await make_rule()
    filler = await _fill(rule)
    for name in ("first", "second", "third"):
        await request(name)
    assert await worker_runner.drive() == 0
    await _kill(filler)
    await _passes()
    assert [row[:3] for row in starts(rule)][1:] == [
        ("started", None, "first"), ("queued", None, "second"), ("queued", None, "third")]
    await _until_working(starts(rule)[1][3])
    await _passes()
    assert [row[0] for row in starts(rule)] == ["no_result", "started", "queued", "queued"]


# (d) the owner is paused while its start is queued and a slot frees: nothing starts.
@pytest.mark.asyncio
async def test_a_paused_owner_skips_its_queued_start_when_a_slot_frees(full, monkeypatch):
    from automation import worker_runner

    rule = await make_rule()
    filler = await _fill(rule)
    await request()
    await worker_runner.drive()
    monkeypatch.setattr("plans.public.thread_paused_since",
                        lambda _db, thread: "2026-01-01" if thread == OWNER else None)
    before = _stored()
    await _kill(filler)
    await _passes()
    assert starts(rule)[1] == ("skipped", "owner paused", "pr-1@a1", None)
    assert _stored()[1] == before[1] and _working() == 0


# (e) a delegate takes the slot between claim and spawn: the start goes back, keeps its place,
# and leaves no thread and no input behind.
@pytest.mark.asyncio
async def test_a_slot_taken_after_the_claim_requeues_the_start_in_place(full, monkeypatch):
    from automation import worker_runner
    from infra.database import get_db
    from tools import delegate_task

    rule = await make_rule()
    await request()
    original, raced = delegate_task._spawn, []

    async def racing(parent_id, parent, task, kwargs, *, rule_start=None):
        if rule_start is not None and not raced:
            delegate = await original(OWNER, parent, "a delegate's task", {"model": "claude-code/sonnet"})
            raced.append(await _until_working(delegate["thread_id"]))
        return await original(parent_id, parent, task, kwargs, rule_start=rule_start)

    monkeypatch.setattr(delegate_task, "_spawn", racing)
    assert await worker_runner.drive() == 0
    assert starts(rule) == [("queued", None, "pr-1@a1", None)] and owner_wakes() == []
    db = get_db()
    assert [row[0] for row in db.execute("SELECT id FROM threads WHERE spawned_by=?", (OWNER,))] == raced
    assert {row[0] for row in db.execute("SELECT DISTINCT thread_id FROM messages")} <= {OWNER, *raced}
    assert not (full["folder"] / "workers").exists() or not any((full["folder"] / "workers").iterdir())

    await _kill(raced[0])
    await _passes()
    assert starts(rule)[0][0] == "started"


# A delivery held by a filter or for its project (no worker) never takes or hides a slot.
@pytest.mark.asyncio
async def test_held_delivery_rows_never_affect_the_claim(full):
    from automation import worker_runner
    from infra.database import write_transaction

    rule = await make_rule()
    now = datetime.now(timezone.utc).isoformat()
    with write_transaction(None, scope="rule_worker_held_rows") as db:
        for n in range(5):
            db.execute(
                "INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,"
                "rule_id,thread_id,outcome,via,reason) VALUES (?,?,'qa','request',"
                "'request',?,?,'held','rule','out of project')", (now, f"held-{n}", rule, OWNER))
    await request()
    assert await worker_runner.drive() == 1
    assert [row[0] for row in starts(rule)] == ["held"] * 5 + ["started"]


# A rule's waiting starts are capped as its script runs are: a match past the cap is skipped,
# and the skip shows in explain and list.
@pytest.mark.asyncio
async def test_a_rules_queued_starts_are_capped_and_a_skip_shows_in_explain_and_list(full, monkeypatch):
    from automation import worker_runner
    from events import rule_runs

    monkeypatch.setattr(rule_runs, "QUEUE_CAP", 2)
    rule = await make_rule()
    await _fill(rule)
    events = [await request(f"pr-{n}@a1") for n in (2, 3, 4)]
    assert await worker_runner.drive() == 0
    assert [row[:2] for row in starts(rule)][1:] == [
        ("queued", None), ("queued", None), ("skipped", "queue full")]
    explained = await atool(OWNER, operation="explain", event_id=events[2])
    assert [d["explanation"] for d in explained["decisions"]] == [
        f"rule {rule} did not start a worker for {OWNER}: queue full"]
    [listed] = (await atool(OWNER, operation="list"))["rules"]
    assert (listed["starts_24h"], listed["skips_24h"]) == (3, 1)
