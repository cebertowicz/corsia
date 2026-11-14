from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from events.connectors import github_head_checks as checks
from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse
from tests.test_github_pr_facts import _rule

READ_FACT = {"field": "draft", "op": "present"}


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
        return replies.pop(0), None

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
async def test_a_webhook_repository_emits_through_the_broker(monkeypatch):
    from connections.github_broker import GithubReadBroker
    from events.connectors.github_read_budget import budget

    budget.clear()
    calls, sink = [], Sink()
    monkeypatch.setattr(poller, "_locks", {})
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: {"reads": []})
    monkeypatch.setattr(poller, "_save_state", lambda *_args: None)

    def run(args, *, timeout):
        calls.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "HTTP/2.0 200 OK\r\n\r\n" + json.dumps(response().body), "")

    agg = checks.HeadCheckAggregator(GithubReadBroker(run), sink, sleep=lambda _: asyncio.sleep(0))
    agg.observe(payload())
    await finish(agg)
    assert calls == ["repos/o/r/commits/abc/check-suites"]
    assert sink.events[0]["event_type"] == "github.head.checks_completed"
    budget.clear()


@pytest.mark.asyncio
async def test_refused_read_stops_after_one_attempt_with_reason(monkeypatch, caplog):
    from connections.github_broker import GithubReadBroker
    from events.connectors.github_read_budget import budget

    budget.clear()
    monkeypatch.setattr(poller, "_locks", {})
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: {"reads": []})
    monkeypatch.setattr(poller, "_save_state", lambda *_args: None)
    calls, delays, sink = [], [], Sink()

    def refused(args, *, timeout):
        from connections.github_read_request import GithubReadRefused
        calls.append(args)
        raise GithubReadRefused("blocked_for_test")

    async def sleep(delay):
        delays.append(delay)

    agg = checks.HeadCheckAggregator(GithubReadBroker(refused), sink, sleep=sleep)
    with caplog.at_level(logging.WARNING):
        agg.observe(payload())
        await finish(agg)
    assert len(calls) == 1 and len(delays) == 1 and not sink.events
    assert "read_refused: blocked_for_test" in caplog.text
    budget.clear()


@pytest.mark.asyncio
async def test_four_held_reads_keep_pending_and_clear_memory(monkeypatch, caplog):
    calls, sink = [], Sink()

    async def read(*_args):
        calls.append(True)
        return None, "budget_exhausted"

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(sink)
    with caplog.at_level(logging.INFO):
        agg.observe(payload())
        await finish(agg)
    assert len(calls) == 4 and not sink.events
    assert not agg._tasks and not agg._facts
    kept = [r for r in caplog.records if "kept for restart after retries" in r.message]
    assert len(kept) == 1 and kept[0].levelno == logging.WARNING
    assert [entry["head_sha"] for entry in await checks.pending.pending()] == ["abc"]
    await checks.pending.forget("o/r", "abc")


@pytest.mark.asyncio
async def test_many_settled_and_failed_heads_leave_no_state(monkeypatch):
    async def read(_runner, _repo, path):
        return response(503 if "/bad" in path else 200), None

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(Sink())
    for n in range(20):
        agg.observe(payload(f"{'bad' if n % 2 else 'good'}{n}"))
    await finish(agg)
    assert not agg._tasks and not agg._facts
    for n in range(1, 20, 2):
        await checks.pending.forget("o/r", f"bad{n}")


@pytest.mark.asyncio
async def test_sink_failure_is_logged_and_clears_both_maps(monkeypatch, caplog):
    async def read(*_args):
        return response(), None

    class FailingSink:
        async def admit_event(self, **_event):
            raise RuntimeError("intake unavailable")

    monkeypatch.setattr(poller, "gated_read", read)
    agg = aggregator(FailingSink())
    agg.observe(payload())
    await finish(agg)  # never escapes the task
    assert "could not admit settled head" in caplog.text
    assert not agg._tasks and not agg._facts
    await checks.pending.forget("o/r", "abc")


@pytest.mark.asyncio
async def test_a_locked_intake_is_retried_under_the_same_event_id(monkeypatch):
    import sqlite3

    async def read(*_args):
        return response(), None

    class LockedOnceSink(Sink):
        async def admit_event(self, **event):
            self.events.append(event)
            if len(self.events) == 1:
                raise sqlite3.OperationalError("database is locked: write admission for admit_event waited 30s")
            return SimpleNamespace(refusal=None)

    forgotten = []

    async def forget(*key):
        forgotten.append(key)

    monkeypatch.setattr(poller, "gated_read", read)
    monkeypatch.setattr(checks.pending, "forget", forget)
    sink = LockedOnceSink()
    agg = aggregator(sink)
    agg.observe(payload())
    await finish(agg)
    assert len(sink.events) == 2 and sink.events[0]["event_id"] == sink.events[1]["event_id"]
    assert forgotten == [("o/r", "abc")]  # admitted, so not kept for a restart
    assert not agg._tasks and not agg._facts


@pytest.mark.asyncio
async def test_refusal_logged_without_optional_none_facts(monkeypatch, caplog):
    async def read(*_args):
        return response(), None

    monkeypatch.setattr(poller, "gated_read", read)
    sink = Sink(SimpleNamespace(code="unknown_type", text="private details"))
    agg = aggregator(sink)
    with caplog.at_level(logging.INFO):
        agg.observe(payload())
        await finish(agg)
    assert sink.events[0]["facts"] == {"repo": "o/r", "head_sha": "abc", "conclusion": "success"}
    assert "code=unknown_type" in caplog.text and "private details" not in caplog.text
    assert sink.events[0]["origin"] == "connector"
    assert len(sink.events) == 1


@pytest.mark.asyncio
async def test_debounce_replaces_task_without_old_cleanup_losing_new_state(monkeypatch):
    async def read(*_args):
        return response(), None

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
        return replies.pop(0), None

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
    result, reason = await poller.gated_read(lambda *_a: None, "o/r", "path")
    assert result.status == 200 and reason is None
    assert calls == [{"low_priority": True}]
    assert len(saved[0][1]["reads"]) == 1
    state["reads"] *= poller.MAX_HOURLY_READS
    assert await poller.gated_read(lambda *_a: None, "o/r", "path") == (None, "budget_exhausted")
    assert len(calls) == 1 and len(saved) == 2
    assert status == {"state": "starting", "reason": None, "counted_reads": 0, "degraded": None}


@pytest.mark.asyncio
async def test_zero_run_suite_does_not_block(monkeypatch):
    reply = response()
    reply.body["check_suites"].append({"id": 2, "status": "queued", "latest_check_runs_count": 0})

    async def read(*_args):
        return reply, None

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


def test_hook_defaults_on_except_test_mode_or_explicit_disable(monkeypatch):
    monkeypatch.delenv(checks.ENABLE_ENV, raising=False)
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.setenv(poller.DISABLE_ENV, "1")
    assert checks.hook_disabled_reason() is None
    monkeypatch.setenv(checks.ENABLE_ENV, "0")
    assert checks.hook_disabled_reason() == "not_enabled"
    monkeypatch.setenv(checks.ENABLE_ENV, "1")
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    assert checks.hook_disabled_reason() == "test_mode"


async def _settled(monkeypatch, read, number=7):
    monkeypatch.setattr(poller, "gated_read", read)
    sink = Sink()
    agg = aggregator(sink)
    agg.observe(payload(), {"number": number})
    await finish(agg)
    return sink.events[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
async def test_a_held_suite_arrives_as_action_required_whatever_else_failed(isolated_db, monkeypatch, timed_out):
    _rule("head.checks_completed", [READ_FACT])
    found = [{"id": 2, "updated_at": "t", "status": "completed", "conclusion": "action_required",
              "latest_check_runs_count": 0},
             {"id": 3, "updated_at": "t", "status": "completed", "conclusion": "success",
              "latest_check_runs_count": 0}]
    runs = []
    if timed_out:  # one suite held, another timed out
        found.append({"id": 4, "updated_at": "t", "status": "completed", "conclusion": "timed_out",
                      "latest_check_runs_count": 1})
        runs = [{"name": "ci", "status": "completed", "conclusion": "timed_out", "id": 1}]
    paths = []

    async def read(_runner, _repo, path):
        paths.append(path)
        if path.endswith("check-suites"):
            return GhResponse(200, {}, {"check_suites": found}), None
        if "/pulls/" in path:
            return GhResponse(200, {}, {"head": {"sha": "abc"}, "base": {"ref": "dev"}, "mergeable": None}), None
        return GhResponse(200, {}, {"total_count": len(runs), "check_runs": runs}), None

    facts = (await _settled(monkeypatch, read))["facts"]
    assert facts["conclusion"] == "action_required"
    assert facts["pr_number"] == 7 and facts["head_sha"] == "abc"
    assert len(paths) == 2  # suites once, then the PR; no runs read


@pytest.mark.asyncio
async def test_a_head_whose_pull_request_moved_on_is_still_named_by_its_settled_head(isolated_db, monkeypatch):
    _rule("head.checks_completed", [READ_FACT])
    failed = response()
    failed.body["check_suites"][0]["conclusion"] = "failure"
    paths = []

    async def read(_runner, _repo, path):
        paths.append(path)
        if path.endswith("check-suites"):
            return failed, None
        if "/pulls/" in path:  # a push landed after the suites settled on abc
            return GhResponse(200, {}, {"head": {"sha": "new"}, "base": {"ref": "dev"}, "mergeable": True}), None
        runs = [{"name": "ci", "status": "completed", "conclusion": "failure", "id": 1}]
        return GhResponse(200, {}, {"total_count": 1, "check_runs": runs}), None

    event = await _settled(monkeypatch, read)
    assert event["facts"]["head_sha"] == "abc" and event["facts"]["conclusion"] == "failure"
    assert paths == ["repos/o/r/commits/abc/check-suites", "repos/o/r/pulls/7"]
    assert event["event_id"] == checks._event_id("o/r", "abc", failed.body["check_suites"])


@pytest.mark.asyncio
async def test_no_rule_testing_a_read_fact_means_no_read_beyond_the_suites(isolated_db, monkeypatch):
    _rule("head.checks_completed", [{"field": "repo", "op": "eq", "value": "o/r"}])  # tests no read fact
    paths = []

    async def read(_runner, _repo, path):
        paths.append(path)
        return response(), None

    facts = (await _settled(monkeypatch, read))["facts"]
    assert paths == ["repos/o/r/commits/abc/check-suites"]
    assert facts == {"repo": "o/r", "head_sha": "abc", "conclusion": "success", "pr_number": 7}


def _reads_with_a_pull_request(pull):
    async def read(_runner, _repo, path):
        if "/pulls/" in path:
            return GhResponse(200, {}, pull), None
        if path.endswith("check-suites"):
            return response(), None
        runs = [{"name": "ci", "status": "completed", "conclusion": "success", "id": 1}]
        return GhResponse(200, {}, {"total_count": 1, "check_runs": runs}), None

    return read


@pytest.mark.asyncio
async def test_a_rule_naming_a_read_fact_and_a_read_populated_one_fires_after_the_read(isolated_db, monkeypatch):
    from events.rule_match import test_matches

    match = [{"field": "conclusion", "op": "eq", "value": "success"},
             {"field": "draft", "op": "eq", "value": False},
             {"field": "author_origin", "op": "eq", "value": "outside"}]
    _rule("head.checks_completed", match)
    pull = {"head": {"sha": "abc"}, "base": {"ref": "dev"}, "draft": False, "mergeable": True,
            "user": {"login": "someone"}}
    facts = (await _settled(monkeypatch, _reads_with_a_pull_request(pull)))["facts"]
    assert all(test_matches(test, facts) for test in match), facts


@pytest.mark.asyncio
async def test_a_failed_rule_probe_reads_anyway_and_the_head_still_settles(isolated_db, monkeypatch):
    def broken(*_args):
        raise OSError("database is locked")

    monkeypatch.setattr("events.rule_reads.type_rules", broken)
    pull = {"head": {"sha": "abc"}, "base": {"ref": "dev"}, "mergeable": True, "user": {"login": "someone"}}
    event = await _settled(monkeypatch, _reads_with_a_pull_request(pull))
    assert event["facts"]["conclusion"] == "success" and event["facts"]["author_origin"]
