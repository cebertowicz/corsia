"""A head waiting in the debounce window survives a backend restart (no real GitHub)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from events.connectors import github_head_checks as checks
from events.connectors import github_head_pending as pending
from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse

SUITE = {"id": 1, "updated_at": "2026-01-01T00:00:00Z", "status": "completed", "conclusion": "success",
         "latest_check_runs_count": 1}


def payload(sha="abc"):
    return {"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": sha, "head_branch": "feat"}}


class Sink:
    """Dedupes on event_id the way intake does."""

    def __init__(self):
        self.events, self.seen = [], set()

    async def admit_event(self, **event):
        duplicate = event["event_id"] in self.seen
        self.seen.add(event["event_id"])
        if not duplicate:
            self.events.append(event)
        return SimpleNamespace(refusal=None, duplicate=duplicate)


@pytest.fixture(autouse=True)
def reads(monkeypatch):
    async def read(*_args):
        return GhResponse(200, {}, {"check_suites": [SUITE]}), None

    async def facts(_runner, _repo, _number, base):
        return dict(base)

    monkeypatch.setattr(poller, "gated_read", read)
    monkeypatch.setattr(checks.pr_facts, "settled_head_facts", facts)


def aggregator(sink, gate=None):
    async def sleep(_n):
        await (gate.wait() if gate else asyncio.sleep(0))
    return checks.HeadCheckAggregator(lambda *_a, **_kw: None, sink, sleep=sleep)


async def stop(agg):
    for task in list(agg._tasks.values()):
        task.cancel()
    await asyncio.gather(*agg._tasks.values(), return_exceptions=True)  # a restart, not a drop


@pytest.mark.asyncio
async def test_restart_inside_window_emits_exactly_once(monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.setenv(checks.ENABLE_ENV, "1")
    sink, gate = Sink(), asyncio.Event()
    old = aggregator(sink, gate)
    old.observe(payload(), {"number": 7})
    await asyncio.sleep(0.05)
    assert [e["head_sha"] for e in await pending.pending()] == ["abc"]
    await stop(old)  # backend restart: the object is gone, the window never elapsed
    assert sink.events == [] and len(await pending.pending()) == 1

    new = aggregator(sink)
    monkeypatch.setattr(checks, "_default", new)
    assert await checks.rearm_pending_heads() == 1
    await asyncio.gather(*new._tasks.values())
    assert len(sink.events) == 1
    facts = sink.events[0]["facts"]
    assert (facts["head_branch"], facts["pr_number"]) == ("feat", 7)
    assert await pending.pending() == []


async def _rearm(agg):
    entries = await pending.pending()
    for entry in entries:
        agg.rearm(entry)
    return len(entries)


@pytest.mark.asyncio
async def test_head_settled_before_restart_emits_nothing_new():
    sink = Sink()
    first = aggregator(sink)
    first.observe(payload(), None)
    await asyncio.gather(*first._tasks.values())
    assert len(sink.events) == 1 and await pending.pending() == []
    assert await _rearm(aggregator(sink)) == 0
    # A key that outlived its clear (crash between emit and clear) re-arms but dedupes at intake.
    await pending.remember("o/r", "abc", "feat", None)
    again = aggregator(sink)
    assert await _rearm(again) == 1
    await asyncio.gather(*again._tasks.values())
    assert len(sink.events) == 1 and await pending.pending() == []


@pytest.mark.asyncio
async def test_dropped_head_is_cleared(monkeypatch):
    async def refused(*_args):
        return None, "read_refused"
    monkeypatch.setattr(poller, "gated_read", refused)
    agg = aggregator(Sink())
    agg.observe(payload(), None)
    await asyncio.gather(*agg._tasks.values())
    assert await pending.pending() == []


@pytest.mark.asyncio
async def test_pr_read_error_is_logged_not_raised(monkeypatch, caplog):
    async def facts_down(*_args):
        raise RuntimeError("pr read down")
    monkeypatch.setattr(checks.pr_facts, "settled_head_facts", facts_down)
    agg = aggregator(Sink())
    agg.observe(payload(), {"number": 7})
    task = agg._tasks[("o/r", "abc")]
    await asyncio.gather(task)
    assert task.exception() is None
    assert "could not admit settled head o/r@abc" in caplog.text
    assert agg._tasks == {} and await pending.pending() == []


@pytest.mark.asyncio
async def test_stale_keys_are_never_rearmed_and_are_pruned():
    await pending.remember("o/r", "old", None, None)
    await pending.remember("o/r", "new", None, None)

    def age():
        from infra.settings import get_setting, set_setting
        row = get_setting(pending.SETTING_KEY)
        row["o/r@old"]["at"] = time.time() - pending.MAX_AGE_SECONDS - 1
        set_setting(pending.SETTING_KEY, row)
    age()
    assert [e["head_sha"] for e in await pending.pending()] == ["new"]
    from infra.settings import get_setting
    assert list(get_setting(pending.SETTING_KEY)) == ["o/r@new"]
    await pending.forget("o/r", "new")
    assert get_setting(pending.SETTING_KEY) == {}


@pytest.mark.asyncio
async def test_rearm_pending_heads_is_off_in_test_mode(monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    await pending.remember("o/r", "abc", None, None)
    assert await checks.rearm_pending_heads() == 0
    await pending.forget("o/r", "abc")
