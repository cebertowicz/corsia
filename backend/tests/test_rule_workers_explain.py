"""``explain`` tells each worker start in its own words, and ``list`` counts the starts (R25).

The held start is told in ``test_rule_workers_hold``; a refused launch's own causes in
``test_rule_workers_start``.
"""

from __future__ import annotations

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, atool, make_rule, request, settle_all, world,
)


async def _told(rule: str, event: str) -> list[str]:
    explained = await atool(OWNER, operation="explain", event_id=event)
    return [d["explanation"] for d in explained["decisions"] if d["rule_id"] == rule]


@pytest.mark.asyncio
async def test_explain_tells_each_start_and_list_counts_them(world, monkeypatch):
    from automation import worker_runner

    paused = {"on": False}
    monkeypatch.setattr("plans.public.thread_paused_since",
                        lambda _db, thread: "2026-01-01" if paused["on"] and thread == OWNER else None)
    rule = await make_rule()
    first = await request()
    assert await _told(rule, first) == [f"rule {rule} will start a worker for {OWNER} with it"]
    await worker_runner.drive()
    assert await _told(rule, first) == [f"rule {rule} started a worker for {OWNER}"]

    again = await request()
    assert await _told(rule, again) == [
        f"rule {rule} did not start a worker for {OWNER}: duplicate request"]
    await settle_all()
    assert await _told(rule, first) == [f"rule {rule}'s worker for {OWNER} returned a result"]

    paused["on"] = True
    while_paused = await request("pr-2@b1")
    await settle_all()
    assert await _told(rule, while_paused) == [
        f"rule {rule} did not start a worker for {OWNER}: owner paused"]

    paused["on"] = False
    outside = await request("pr-3@c1", staging_path="../../..")
    await settle_all()
    [refused] = await _told(rule, outside)
    assert refused.startswith(f"rule {rule} did not start a worker for {OWNER}: "
                              "staging_path ../../.. is outside your staging folder")

    [listed] = (await atool(OWNER, operation="list"))["rules"]
    assert (listed["starts_24h"], listed["skips_24h"], listed["wakes_24h"]) == (1, 2, 0)
    assert listed["last_fired"] is not None
