from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from events.connectors import github_head_checks as checks
from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse


def payload(sha="abc"):
    return {"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": sha}}


def response(status=200):
    return GhResponse(status, {}, {"check_suites": [
        {"id": 1, "updated_at": "2026-01-01T00:00:00Z", "status": "completed", "conclusion": "success", "latest_check_runs_count": 1}
    ]})


class Sink:
    def __init__(self, refusal=None):
        self.events = []
        self.refusal = refusal

    async def admit_event(self, **event):
        self.events.append(event)
        return SimpleNamespace(refusal=self.refusal)


def aggregator(sink):
    return checks.HeadCheckAggregator(lambda *_a, **_kw: None, sink, sleep=lambda _n: asyncio.sleep(0))


async def finish(agg):
    await asyncio.gather(*list(agg._tasks.values()))


@pytest.mark.asyncio
async def test_held_read_retries_then_emits_once(monkeypatch):
    replies, delays, sink = [None, response()], [], Sink()

    async def read(*_args):
        return replies.pop(0)

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(sink)
    agg.sleep = sleep
    agg.observe(payload())
    await finish(agg)
    assert delays == [30, 60]
    assert len(sink.events) == 1
    assert not agg._tasks and not agg._facts


@pytest.mark.asyncio
async def test_four_held_reads_drop_and_clear_state(monkeypatch, caplog):
    calls, sink = [], Sink()

    async def read(*_args):
        calls.append(True)
        return None

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(sink)
    with caplog.at_level(logging.INFO):
        agg.observe(payload())
        await finish(agg)
    assert len(calls) == 4 and not sink.events
    assert not agg._tasks and not agg._facts
    drops = [r for r in caplog.records if "dropped after retries" in r.message]
    assert len(drops) == 1 and drops[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_many_settled_and_failed_heads_leave_no_state(monkeypatch):
    async def read(_runner, _repo, path):
        return response(503 if "/bad" in path else 200)

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(Sink())
    for n in range(20):
        agg.observe(payload(f"{'bad' if n % 2 else 'good'}{n}"))
    await finish(agg)
    assert not agg._tasks and not agg._facts


@pytest.mark.asyncio
async def test_sink_failure_clears_both_maps(monkeypatch):
    async def read(*_args):
        return response()

    class FailingSink:
        async def admit_event(self, **_event):
            raise RuntimeError("intake unavailable")

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(FailingSink())
    agg.observe(payload())
    with pytest.raises(RuntimeError, match="intake unavailable"):
        await finish(agg)
    assert not agg._tasks and not agg._facts


@pytest.mark.asyncio
async def test_refusal_logged_without_optional_none_facts(monkeypatch, caplog):
    async def read(*_args):
        return response()

    monkeypatch.setattr(poller, "gated_read", read)
    sink = Sink(SimpleNamespace(code="unknown_type", text="private details"))
    agg = aggregator(sink)
    with caplog.at_level(logging.INFO):
        agg.observe(payload())
        await finish(agg)
    assert sink.events[0]["facts"] == {"repo": "o/r", "head_sha": "abc", "conclusion": "success"}
    assert "code=unknown_type" in caplog.text and "private details" not in caplog.text
    assert sink.events[0]["origin"] == "connector"


@pytest.mark.asyncio
async def test_debounce_replaces_task_without_old_cleanup_losing_new_state(monkeypatch):
    async def read(*_args):
        return response()

    monkeypatch.setattr(poller, "gated_read", read)
    sink = Sink()
    agg = aggregator(sink)
    agg.observe(payload())
    await asyncio.sleep(0)
    agg.observe(payload())
    await finish(agg)
    assert len(sink.events) == 1
    assert not agg._facts and not agg._tasks


@pytest.mark.asyncio
async def test_pending_suite_waits_and_cancelled_conclusion_is_preserved(monkeypatch, caplog):
    pending = response()
    pending.body["check_suites"][0]["status"] = "queued"
    cancelled = response()
    cancelled.body["check_suites"][0]["conclusion"] = "cancelled"
    replies = [pending, cancelled]

    async def read(*_args):
        return replies.pop(0)

    monkeypatch.setattr(poller, "gated_read", read)
    sink = Sink()
    agg = aggregator(sink)
    with caplog.at_level(logging.INFO):
        agg.observe(payload())
        await finish(agg)
    assert "incomplete suites=1" in caplog.text
    assert not sink.events and not agg._facts and not agg._tasks
    agg.observe(payload())
    await finish(agg)
    assert sink.events[0]["facts"]["conclusion"] == "cancelled"


@pytest.mark.asyncio
async def test_gated_read_uses_and_saves_existing_repo_budget(monkeypatch):
    state = {"reads": []}
    saved, calls = [], []
    status = {"state": "starting", "reason": None, "counted_reads": 0, "degraded": None}
    monkeypatch.setattr(poller, "_status", status)
    monkeypatch.setattr(poller, "_locks", {})
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: state)
    monkeypatch.setattr(poller, "_save_state", lambda repo, value: saved.append((repo, value.copy())))
    monkeypatch.setattr(poller.feed, "gh_get", lambda *a, **kw: calls.append(kw) or response())
    assert (await poller.gated_read(lambda *_a: None, "o/r", "path")).status == 200
    assert calls == [{"low_priority": True}]
    assert len(saved[0][1]["reads"]) == 1
    state["reads"] *= poller.MAX_HOURLY_READS
    assert await poller.gated_read(lambda *_a: None, "o/r", "path") is None
    assert len(calls) == 1 and len(saved) == 2
    assert status == {"state": "starting", "reason": None, "counted_reads": 0, "degraded": None}


@pytest.mark.asyncio
async def test_zero_run_suite_does_not_block(monkeypatch):
    reply = response()
    reply.body["check_suites"].append({"id": 2, "status": "queued", "latest_check_runs_count": 0})

    async def read(*_args):
        return reply

    monkeypatch.setattr(poller, "gated_read", read)
    sink = Sink()
    agg = aggregator(sink)
    agg.observe(payload())
    await finish(agg)
    assert len(sink.events) == 1


@pytest.mark.asyncio
async def test_missing_pr_does_not_erase_selected_identity():
    agg = aggregator(Sink())
    first = payload()
    from events.identity import check_suite_pr_metadata_from_event

    prs = [{"number": n, "state": state, "head": {"sha": "abc", "ref": "main"},
            "base": {"ref": "dev"}} for n, state in ((1, "closed"), (7, "open"))]
    first["check_suite"].update(head_branch="main", pull_requests=prs)
    agg.observe(first, check_suite_pr_metadata_from_event(first))
    agg.observe(payload(), None)
    assert agg._facts[("o/r", "abc")] == ("main", 7)
    for task in agg._tasks.values():
        task.cancel()
    await asyncio.gather(*agg._tasks.values(), return_exceptions=True)


def test_event_identity_depends_on_suites_not_delivery():
    suites = response().body["check_suites"]
    assert checks._event_id("o/r", "abc", suites) == checks._event_id("o/r", "abc", list(reversed(suites)))
    assert checks._event_id("o/r", "abc", suites) != checks._event_id("o/r", "abc", [{**suites[0], "updated_at": "later"}])


def test_hook_requires_both_switches(monkeypatch):
    monkeypatch.setenv(checks.ENABLE_ENV, "1")
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    assert checks.hook_disabled_reason() == "test_mode"
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.setenv(poller.DISABLE_ENV, "1")
    assert checks.hook_disabled_reason() is None
    monkeypatch.delenv(checks.ENABLE_ENV)
    assert checks.hook_disabled_reason() == "not_enabled"
