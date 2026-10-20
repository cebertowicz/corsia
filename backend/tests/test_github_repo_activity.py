from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from events.connectors import github_head_checks as checks
from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse


class Sink:
    def __init__(self):
        self.events = []

    async def admit_event(self, **event):
        self.events.append(event)
        return type("Result", (), {"accepted": True, "duplicate": False,
                                    "refusal": None, "rules_matched": []})()


def suite(identifier, status="completed", conclusion="success", attempt=1):
    return {"id": identifier, "status": status, "conclusion": conclusion,
            "updated_at": f"2026-01-01T00:00:0{identifier}Z", "run_attempt": attempt,
            "app": {"id": identifier}, "workflow_run": {"workflow_id": identifier}}


@pytest.mark.asyncio
async def test_debounce_aggregates_three_suites_once_and_allows_no_pr(monkeypatch):
    reads, sink = [], Sink()
    response = GhResponse(200, {}, {"check_suites": [suite(1), suite(2), suite(3)]})
    monkeypatch.setattr(checks.feed, "gh_get", lambda *args: reads.append(args) or response)
    aggregator = checks.HeadCheckAggregator(lambda *_args, **_kwargs: None, sink, sleep=lambda _n: asyncio.sleep(0))
    payload = {"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": "a", "head_branch": "main"}}
    aggregator.observe(payload); aggregator.observe(payload); aggregator.observe(payload)
    await asyncio.sleep(0.02)
    assert len(reads) == 1 and sink.events[0]["facts"] == {
        "repo": "o/r", "head_sha": "a", "head_branch": "main", "pr_number": None, "conclusion": "success"}


@pytest.mark.asyncio
async def test_pending_emits_nothing_and_rerun_changes_event_id(monkeypatch):
    sink = Sink()
    responses = [GhResponse(200, {}, {"check_suites": [suite(1, "queued")]}),
                 GhResponse(200, {}, {"check_suites": [suite(1, attempt=2)]})]
    monkeypatch.setattr(checks.feed, "gh_get", lambda *_args: responses.pop(0))
    aggregator = checks.HeadCheckAggregator(lambda *_args, **_kwargs: None, sink, sleep=lambda _n: asyncio.sleep(0))
    payload = {"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": "a", "pull_requests": [{"number": 7}]}}
    aggregator.observe(payload); await asyncio.sleep(0.02)
    assert not sink.events
    aggregator.observe(payload); await asyncio.sleep(0.02)
    first = sink.events[0]["event_id"]
    assert sink.events[0]["external_id"] == "o/r#7"
    assert first != checks._event_id("o/r", "a", [suite(1)])


@pytest.mark.asyncio
async def test_cancelled_is_preserved(monkeypatch):
    sink = Sink()
    monkeypatch.setattr(checks.feed, "gh_get", lambda *_args: GhResponse(200, {}, {"check_suites": [suite(1, conclusion="cancelled")]}))
    aggregator = checks.HeadCheckAggregator(lambda *_args, **_kwargs: None, sink, sleep=lambda _n: asyncio.sleep(0))
    aggregator.observe({"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": "a"}})
    await asyncio.sleep(0.02)
    assert sink.events[0]["facts"]["conclusion"] == "cancelled"


@pytest.mark.asyncio
async def test_poller_skips_recent_webhook_and_cap_holds_cursor(monkeypatch):
    saved = []
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_args: True)
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: {"issues_cursor": "saved"})
    monkeypatch.setattr(poller, "_save_state", lambda *_args: saved.append(True))
    assert not await poller._poll_repo(lambda *_args: None, "o/r", datetime.now(UTC))
    assert not saved  # webhook stand-down never advances a saved cursor
    state = {"reads": ["2999-01-01T00:00:00Z"] * poller.MAX_HOURLY_READS, "cursor": "hold"}
    assert await poller._get(lambda *_args: None, "x", state, datetime.now(UTC)) is None
    assert state["cursor"] == "hold"


def test_startup_boundary_uses_webhook_overlap(monkeypatch):
    from infra import webhook_delivery_log
    monkeypatch.setattr(webhook_delivery_log, "repo_last_delivered_at", lambda _repo: "2026-01-03T02:00:00Z")
    assert poller._startup_boundary("o/r", {"issues_cursor_at": "2026-01-01T00:00:00Z"}) == "2026-01-03T01:50:00Z"


@pytest.mark.asyncio
async def test_startup_catchup_marks_cursor_lost_after_three_pages(monkeypatch):
    state, saved = {"issues_cursor": "old", "reads": []}, []
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: state)
    monkeypatch.setattr(poller, "_startup_boundary", lambda *_args: "2026-01-01T00:00:00Z")
    monkeypatch.setattr(poller, "_save_state", lambda *_args: saved.append(True))
    monkeypatch.setattr(poller, "_get", lambda *_args: asyncio.sleep(0, result=GhResponse(200, {}, [{"created_at": "2026-01-02T00:00:00Z"}])))
    assert not await poller._startup_catchup(lambda *_args: None, "o/r", datetime(2026, 1, 4, tzinfo=UTC))
    assert poller.poller_status()["degraded"] == "cursor_lost" and saved and state["issues_cursor_at"] == "2026-01-04T00:00:00Z"


def test_304_is_not_counted():
    state = {"reads": []}
    poller._counted(GhResponse(304, {}, None), state, datetime.now(UTC))
    assert state["reads"] == []


@pytest.mark.asyncio
async def test_rolling_expired_reads_unblock_the_next_request(monkeypatch):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    state, calls = {"reads": ["2025-12-31T00:00:00Z"] * poller.MAX_HOURLY_READS}, []
    monkeypatch.setattr(poller.feed, "gh_get", lambda *_args: calls.append(True) or GhResponse(304, {}, None))
    assert (await poller._get(lambda *_args: None, "x", state, now)).status == 304
    assert calls == [True] and state["reads"] == []


@pytest.mark.asyncio
async def test_low_remaining_stops_later_reads_and_holds_cursor(monkeypatch):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    state, calls = {"reads": [], "issues_cursor": "held"}, []
    monkeypatch.setattr(poller.feed, "gh_get", lambda *_args: calls.append(True) or GhResponse(200, {"x-ratelimit-remaining": "999"}, []))
    assert (await poller._get(lambda *_args: None, "first", state, now)).status == 200
    assert await poller._get(lambda *_args: None, "second", state, now) is None
    assert calls == [True] and state["issues_cursor"] == "held"
    assert poller.poller_status()["degraded"] == "rate_limited"


@pytest.mark.asyncio
async def test_previous_head_closed_merged_and_dedupe_shapes(monkeypatch):
    state = {"heads": {"3": "old"}, "closed": {}, "pending": [], "reads": []}
    emitted = []
    open_pull = {"number": 3, "head": {"sha": "new"}}
    merged = {"number": 4, "state": "closed", "merged": True, "updated_at": "now", "head": {"sha": "m"}}
    closed = {"number": 5, "state": "closed", "merged": False, "updated_at": "now", "head": {"sha": "c"}}
    answers = [GhResponse(200, {}, []), GhResponse(200, {}, [open_pull]),
               GhResponse(200, {}, [merged, closed]), GhResponse(200, {}, {"workflow_runs": []}),
               GhResponse(200, {}, {"workflow_runs": []})]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_args: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_args: None)
    monkeypatch.setattr(poller, "_get", lambda *_args: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda event, payload, key: emitted.append((event, payload, key)) or asyncio.sleep(0))
    await poller._poll_repo(lambda *_args: None, "o/r", datetime.now(UTC))
    assert [row[1]["action"] for row in emitted] == ["synchronize", "closed", "closed"]
    assert emitted[0][2] == "synchronize:o/r#3:new"
    assert emitted[1][1]["pull_request"]["merged"] is True
    assert emitted[2][1]["pull_request"]["merged"] is False


@pytest.mark.asyncio
async def test_startup_catchup_processes_only_webhook_overlap_without_persisting_it(monkeypatch):
    state, emitted, saved = {"issues_cursor": "three-days-old", "issues_cursor_at": "2026-01-01T00:00:00Z"}, [], []
    rows = [{"id": "new", "created_at": "2026-01-03T01:55:00Z", "event": "labeled", "issue": {"pull_request": {}}},
            {"id": "floor", "created_at": "2026-01-03T01:50:00Z", "event": "labeled", "issue": {"pull_request": {}}},
            {"id": "historic", "created_at": "2026-01-03T01:40:00Z", "event": "labeled", "issue": {"pull_request": {}}}]
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: state)
    monkeypatch.setattr(poller, "_startup_boundary", lambda *_args: "2026-01-03T01:50:00Z")
    monkeypatch.setattr(poller, "_get", lambda *_args: asyncio.sleep(0, result=GhResponse(200, {}, rows)))
    monkeypatch.setattr(poller, "_save_state", lambda *_args: saved.append(True))
    monkeypatch.setattr(poller, "_admit", lambda _event, _payload, key: emitted.append(key) or asyncio.sleep(0))
    assert await poller._startup_catchup(lambda *_args: None, "o/r", datetime(2026, 1, 3, 2, tzinfo=UTC))
    assert emitted == ["labeled:o/r:new"] and not saved and "issues_boundary" not in state


@pytest.mark.asyncio
async def test_normal_issue_cursor_traverses_pages_and_never_replays_history(monkeypatch):
    state, emitted = {"issues_cursor": "old", "reads": []}, []
    pages = [GhResponse(200, {}, [{"id": "new", "created_at": "2026-01-02T00:00:00Z", "event": "labeled", "issue": {"pull_request": {}}}]),
             GhResponse(200, {}, [{"id": "old", "created_at": "2026-01-01T00:00:00Z", "event": "labeled", "issue": {"pull_request": {}}}])]
    monkeypatch.setattr(poller, "_get", lambda *_args: asyncio.sleep(0, result=pages.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda _event, _payload, key: emitted.append(key) or asyncio.sleep(0))
    assert await poller._issue_events(lambda *_args: None, "o/r", state, datetime(2026, 1, 2, tzinfo=UTC))
    assert emitted == ["labeled:o/r:new"] and state["issues_cursor"] == "new"


@pytest.mark.asyncio
async def test_settled_head_admits_only_completed_suites(monkeypatch):
    state, emitted = {"pending": ["a"], "reads": []}, []
    answers = [GhResponse(200, {}, []), GhResponse(200, {}, []), GhResponse(200, {}, []),
               GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []}),
               GhResponse(200, {}, {"check_suites": [suite(1, "queued"), suite(2)]})]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_args: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_args: None)
    monkeypatch.setattr(poller, "_get", lambda *_args: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda event, _payload, key: emitted.append((event, key)) or asyncio.sleep(0))
    await poller._poll_repo(lambda *_args: None, "o/r", datetime.now(UTC))
    assert emitted == [("check_suite", "check_suite:o/r:2")]


@pytest.mark.asyncio
async def test_transient_sink_error_propagates(monkeypatch):
    class FailingSink:
        async def admit_event(self, **_event):
            raise RuntimeError("transient database failure")
    monkeypatch.setattr(checks.feed, "gh_get", lambda *_args: GhResponse(200, {}, {"check_suites": [suite(1)]}))
    aggregator = checks.HeadCheckAggregator(lambda *_args: None, FailingSink(), sleep=lambda _n: asyncio.sleep(0))
    key = ("o/r", "a")
    aggregator._facts[key] = (None, None)
    with pytest.raises(RuntimeError, match="transient"):
        await aggregator._settle(key)
