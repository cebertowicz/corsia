"""R23: no count of working agents holds a rule's start back (design §7.4).

Every queued start is claimed in the pass that sees it, however many Claude Code processes are
working, and more than one claim batch's worth start in one pass. A rule's own waiting starts
are still bounded by ``rule_runs.QUEUE_CAP``, and held delivery rows never reach the claim.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, atool, make_rule, request, starts, world,
)


def _busy(count: int) -> None:
    """Hold ``count`` Claude Code processes working a turn."""
    from subagents.cc_runtime_state import _LIVE, _Live

    for index in range(count):
        _LIVE.put(f"busy-{index}", _Live(session=None, parent_thread_id=None,
                                         name=f"busy-{index}", turn_active=True))


@pytest.mark.asyncio
async def test_every_queued_start_starts_while_many_agents_work(world):
    from automation import worker_runner
    from subagents.cc_idle_retirement import working_thread_ids

    world["reply"].write_text("HANG")
    _busy(9)
    assert len(working_thread_ids()) == 9
    rule = await make_rule()
    count = worker_runner.BATCH + 1
    for n in range(count):
        await request(f"pr-{n}@a1")
    assert await worker_runner.drive() == count
    assert [row[0] for row in starts(rule)] == ["started"] * count
    assert all(row[3] is not None for row in starts(rule))


@pytest.mark.asyncio
async def test_a_delegation_and_a_rule_start_succeed_with_nine_real_workers_working(world):
    """Nine real Claude Code workers, delegated through the tool and never answering, are
    working; a tenth delegation and a rule-started worker both start."""
    import asyncio
    import json

    from automation import worker_runner
    from subagents.cc_idle_retirement import working_thread_ids
    from tools.delegate_task import DelegateTaskTool

    world["reply"].write_text("HANG")

    async def delegate(n: int) -> str:
        receipt = json.loads(await DelegateTaskTool().execute(
            task=f"Work {n}", model="claude-code/sonnet", plan_node_id="none", _thread_id=OWNER))
        assert receipt["ok"], receipt
        return receipt["thread_id"]

    workers = [await delegate(n) for n in range(9)]
    for _ in range(300):
        if set(workers) <= set(working_thread_ids()):
            break
        await asyncio.sleep(0.02)
    assert set(workers) <= set(working_thread_ids())
    tenth = await delegate(9)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    [(outcome, _reason, _identity, worker)] = starts(rule)
    assert outcome == "started" and worker not in {*workers, tenth, None}


# A delivery held by a filter or for its project (no worker) never reaches the claim.
@pytest.mark.asyncio
async def test_held_delivery_rows_never_affect_the_claim(world):
    from automation import worker_runner
    from infra.database import write_transaction

    world["reply"].write_text("HANG")
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
async def test_a_rules_queued_starts_are_capped_and_a_skip_shows_in_explain_and_list(world, monkeypatch):
    from automation import worker_runner
    from events import rule_runs

    world["reply"].write_text("HANG")
    monkeypatch.setattr(rule_runs, "QUEUE_CAP", 2)
    rule = await make_rule()
    events = [await request(f"pr-{n}@a1") for n in (2, 3, 4)]
    assert [row[:2] for row in starts(rule)] == [
        ("queued", None), ("queued", None), ("skipped", "queue full")]
    explained = await atool(OWNER, operation="explain", event_id=events[2])
    assert [d["explanation"] for d in explained["decisions"]] == [
        f"rule {rule} did not start a worker for {OWNER}: queue full"]
    [listed] = (await atool(OWNER, operation="list"))["rules"]
    assert (listed["starts_24h"], listed["skips_24h"]) == (2, 1)
    assert await worker_runner.drive() == 2


@pytest.mark.asyncio
async def test_a_start_behind_a_full_batch_of_skips_starts_in_the_same_pass(world, monkeypatch):
    from automation import worker_runner
    from events import rule_runs
    from infra.database import get_db

    world["reply"].write_text("HANG")
    rule = await make_rule()
    for n in range(worker_runner.BATCH + 1):
        await request(f"pr-{n}@a1")
    ids = [row[0] for row in get_db().execute(
        "SELECT id FROM event_route_decisions WHERE rule_id=? ORDER BY id", (rule,)).fetchall()]
    skipped_ids = set(ids[:worker_runner.BATCH])
    real_skip = worker_runner._skip
    monkeypatch.setattr(
        worker_runner, "_skip",
        lambda conn, row: rule_runs.TURNED_OFF if row["id"] in skipped_ids else real_skip(conn, row),
    )
    assert await worker_runner.drive() == 1
    assert [row[0] for row in starts(rule)] == ["skipped"] * worker_runner.BATCH + ["started"]
