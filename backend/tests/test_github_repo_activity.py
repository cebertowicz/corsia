from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from events.connectors import github_head_checks as checks
from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse


@pytest.fixture(autouse=True)
def _memory_state(monkeypatch):
    """Repo state lives in memory and the shared gate starts open for every test."""
    stored = {}
    monkeypatch.setattr(poller, "_repo_state", lambda repo: stored.setdefault(repo, {}))
    monkeypatch.setattr(poller, "_save_state", lambda repo, state: stored.__setitem__(repo, state))
    monkeypatch.setattr(poller, "_status", {"state": "starting", "reason": None, "counted_reads": 0,
                                            "backoff": False, "backoff_until": None, "degraded": None})
    monkeypatch.setattr(poller, "_locks", {})
    return stored


class Sink:
    def __init__(self):
        self.events = []

    async def admit_event(self, **event):
        self.events.append(event)
        return type("Result", (), {"accepted": True, "duplicate": False,
                                    "refusal": None, "rules_matched": []})()


def suite(identifier, status="completed", conclusion="success", updated_at=None, runs=1):
    # GET commits/{sha}/check-suites shape: no run_attempt, no workflow_run.
    return {"id": identifier, "head_sha": "a", "status": status, "conclusion": conclusion,
            "updated_at": updated_at or f"2026-01-01T00:00:0{identifier}Z", "pull_requests": [],
            "app": {"id": 15368, "slug": "github-actions"}, "latest_check_runs_count": runs}


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
                 GhResponse(200, {}, {"check_suites": [suite(1, updated_at="2026-01-01T01:00:00Z")]})]
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
    full_page = [{"created_at": "2026-01-02T00:00:00Z"}] * 100  # a short page would end the list
    monkeypatch.setattr(poller, "_get", lambda *_args, **_kw: asyncio.sleep(0, result=GhResponse(200, {}, full_page)))
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
    monkeypatch.setattr(poller, "_get", lambda *_args, **_kw: asyncio.sleep(0, result=answers.pop(0)))
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
    monkeypatch.setattr(poller, "_get", lambda *_args, **_kw: asyncio.sleep(0, result=GhResponse(200, {}, rows)))
    monkeypatch.setattr(poller, "_save_state", lambda *_args: saved.append(True))
    monkeypatch.setattr(poller, "_admit", lambda _event, _payload, key: emitted.append(key) or asyncio.sleep(0))
    assert await poller._startup_catchup(lambda *_args: None, "o/r", datetime(2026, 1, 3, 2, tzinfo=UTC))
    assert emitted == ["labeled:o/r:new"] and not saved and "issues_boundary" not in state


@pytest.mark.asyncio
async def test_normal_issue_cursor_traverses_pages_and_never_replays_history(monkeypatch):
    state, emitted = {"issues_cursor": "old", "reads": []}, []
    pages = [GhResponse(200, {}, [{"id": "new", "created_at": "2026-01-02T00:00:00Z", "event": "labeled", "issue": {"pull_request": {}}}]),
             GhResponse(200, {}, [{"id": "old", "created_at": "2026-01-01T00:00:00Z", "event": "labeled", "issue": {"pull_request": {}}}])]
    monkeypatch.setattr(poller, "_get", lambda *_args, **_kw: asyncio.sleep(0, result=pages.pop(0)))
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
    monkeypatch.setattr(poller, "_get", lambda *_args, **_kw: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda event, _payload, key: emitted.append((event, key)) or asyncio.sleep(0))
    await poller._poll_repo(lambda *_args: None, "o/r", datetime.now(UTC))
    assert emitted == [("check_suite", "check_suite:o/r:2:2026-01-01T00:00:02Z")]


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


# Integration review repros: each failed on 78564172.

@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [GhResponse(304, {}, None), None, GhResponse(500, {}, None)])
@pytest.mark.parametrize("complete_pages", [0, 1, 2])
async def test_304_on_issue_events_is_not_cursor_lost(monkeypatch, stop, complete_pages):
    state = {"issues_cursor": "42", "issues_cursor_at": "2026-01-01T00:00:00Z", "reads": []}
    original = dict(state)
    answers = [GhResponse(200, {}, [{"id": "new"}])] * complete_pages + [stop]
    monkeypatch.setattr(poller, "_save_state", lambda *_args: None)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    poller._status["degraded"] = None
    await poller._issue_events(lambda *_a: None, "o/r", state, datetime(2026, 1, 4, tzinfo=UTC))
    assert poller._status["degraded"] is None
    assert state["issues_cursor_at"] == "2026-01-01T00:00:00Z"
    assert state == original and answers == []


@pytest.mark.asyncio
async def test_first_poll_does_not_replay_history(monkeypatch):
    state, emitted = {}, []
    labeled = {"id": 1, "created_at": "2026-01-01T00:00:00Z", "event": "labeled", "issue": {"pull_request": {}}}
    answers = [GhResponse(200, {}, [labeled]), GhResponse(200, {}, [{"number": 3, "head": {"sha": "h"}}]),
               GhResponse(200, {}, [{"number": 4, "state": "closed", "updated_at": "t"}]),
               GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []})]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_a: None)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda e, p, k: emitted.append(k) or asyncio.sleep(0))
    await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert emitted == []
    assert state["issues_cursor"] == "1" and state["heads"] == {"3": "h"} and state["closed"] == {"4": "t"}


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_runs", [0, 1])
async def test_budget_stop_keeps_pending_heads(monkeypatch, complete_runs):
    state = {"pending": ["a"], "heads": {}, "closed": {}, "issues_cursor": "x", "reads": []}
    answers = [GhResponse(304, {}, None), GhResponse(200, {}, []), GhResponse(200, {}, [])]
    answers += [GhResponse(200, {}, {"workflow_runs": []})] * complete_runs + [None]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_a: None)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda *_a: asyncio.sleep(0))
    await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert state["pending"] == ["a"] and answers == []  # no suite read after either incomplete run read


def _rest_suite(updated_at):
    # Real GET commits/{sha}/check-suites shape: no run_attempt, no workflow_run.
    return {"id": 9, "head_sha": "a", "head_branch": "b", "status": "completed", "conclusion": "success",
            "app": {"id": 15368, "slug": "github-actions"}, "updated_at": updated_at,
            "latest_check_runs_count": 2, "pull_requests": []}


def test_rerun_of_same_suite_gets_new_event_id():
    first = checks._event_id("o/r", "a", [_rest_suite("2026-01-01T00:00:00Z")])
    rerun = checks._event_id("o/r", "a", [_rest_suite("2026-01-01T01:00:00Z")])
    assert first != rerun


def test_admission_hook_is_off_in_test_mode(monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    started = []
    monkeypatch.setattr(checks, "_default", type("A", (), {"observe": lambda self, p: started.append(p)})())
    checks.observe_admitted_check_suite({"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": "a"}})
    assert started == []


# Edges sharing those causes.

def _recording_gh(monkeypatch, answers):
    sent = []
    monkeypatch.setattr(poller.feed, "gh_get", lambda _runner, path, headers=None: sent.append((path, headers)) or answers.pop(0))
    return sent


@pytest.mark.asyncio
async def test_issue_etag_is_kept_only_with_a_committed_cursor(monkeypatch):
    now, page = datetime(2026, 1, 4, tzinfo=UTC), "repos/o/r/issues/events?per_page=100&page=1"
    state = {"issues_cursor": "old", "etags": {page: "e0"}, "reads": []}
    row = {"id": "new", "created_at": "2026-01-03T00:00:00Z", "event": "labeled", "issue": {"pull_request": {}}}
    sent = _recording_gh(monkeypatch, [GhResponse(200, {"etag": "e1"}, [row]), GhResponse(500, {}, None)])
    assert not await poller._issue_events(lambda *_a: None, "o/r", state, now)
    assert sent[0][1] == {"If-None-Match": "e0"} and sent[1][1] is None  # later pages are unconditional
    assert state["etags"][page] == "e0" and state["issues_cursor"] == "old" and not poller._status["degraded"]
    sent = _recording_gh(monkeypatch, [GhResponse(200, {"etag": "e1"}, [row, {"id": "old"}])])
    monkeypatch.setattr(poller, "_admit", lambda *_a: asyncio.sleep(0))
    assert await poller._issue_events(lambda *_a: None, "o/r", state, now)
    assert state["etags"][page] == "e1" and state["issues_cursor"] == "new"


@pytest.mark.asyncio
async def test_empty_first_page_seeds_a_time_floor_so_the_next_label_is_emitted(monkeypatch):
    state, emitted, now = {"reads": []}, [], datetime(2026, 1, 4, tzinfo=UTC)
    row = {"id": "first", "created_at": "2026-01-04T00:01:00Z", "event": "labeled", "issue": {"pull_request": {}}}
    _recording_gh(monkeypatch, [GhResponse(200, {}, []), GhResponse(200, {}, [row])])
    monkeypatch.setattr(poller, "_admit", lambda _e, _p, key: emitted.append(key) or asyncio.sleep(0))
    assert not await poller._issue_events(lambda *_a: None, "o/r", state, now)
    assert state["issues_cursor_at"] == "2026-01-04T00:00:00Z" and "issues_cursor" not in state
    assert await poller._issue_events(lambda *_a: None, "o/r", state, now)
    assert emitted == ["labeled:o/r:first"] and state["issues_cursor"] == "first"


@pytest.mark.asyncio
async def test_startup_catchup_without_cursor_or_boundary_reads_nothing(monkeypatch):
    monkeypatch.setattr(poller, "_startup_boundary", lambda *_a: None)
    sent = _recording_gh(monkeypatch, [])
    assert not await poller._startup_catchup(lambda *_a: None, "o/r", datetime.now(UTC))
    assert sent == []


@pytest.mark.asyncio
async def test_304_open_pulls_and_runs_keep_saved_heads_and_pending(monkeypatch):
    state = {"issues_cursor": "x", "heads": {"3": "h"}, "closed": {}, "pending": ["a"],
             "runs": {"in_progress": ["a"], "queued": []}, "reads": []}
    answers = [GhResponse(304, {}, None)] * 3 + [GhResponse(304, {}, None), GhResponse(304, {}, None)]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    assert await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert answers == [] and state["heads"] == {"3": "h"} and state["pending"] == ["a"]


@pytest.mark.asyncio
async def test_gate_stopped_suite_read_keeps_the_head_pending(monkeypatch):
    state = {"issues_cursor": "x", "heads": {}, "closed": {}, "pending": ["a", "b"], "reads": []}
    answers = [GhResponse(304, {}, None), GhResponse(200, {}, []), GhResponse(200, {}, []),
               GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []}), None]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert answers == [] and state["pending"] == ["a", "b"]


def test_admission_hook_is_off_by_default_and_needs_explicit_enable(monkeypatch):
    started = []
    monkeypatch.setattr(checks, "_default", type("A", (), {"observe": lambda self, p: started.append(p)})())
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(checks.ENABLE_ENV, raising=False)
    checks.observe_admitted_check_suite({})
    assert checks.hook_disabled_reason() == "not_enabled" and started == []
    monkeypatch.setenv(checks.ENABLE_ENV, "1")
    monkeypatch.setenv(poller.DISABLE_ENV, "1")
    checks.observe_admitted_check_suite({})
    assert checks.hook_disabled_reason() == "config_off" and started == []
    monkeypatch.delenv(poller.DISABLE_ENV)
    checks.observe_admitted_check_suite({})
    assert started == [{}]


@pytest.mark.asyncio
async def test_aggregate_read_uses_the_shared_gate(monkeypatch, _memory_state):
    sink, calls = Sink(), []
    monkeypatch.setattr(checks.feed, "gh_get", lambda *a: calls.append(a) or GhResponse(200, {}, {"check_suites": [suite(1)]}))
    aggregator = checks.HeadCheckAggregator(lambda *_a: None, sink, sleep=lambda _n: asyncio.sleep(0))
    aggregator._facts[("o/r", "a")] = (None, None)
    poller._status["backoff_until"] = "2999-01-01T00:00:00Z"
    await aggregator._settle(("o/r", "a"))
    assert calls == [] and sink.events == [] and poller._status["degraded"] == "rate_limited"
    poller._status["backoff_until"] = None
    await aggregator._settle(("o/r", "a"))
    assert calls[0][2] is None and len(sink.events) == 1 and len(_memory_state["o/r"]["reads"]) == 1


@pytest.mark.asyncio
async def test_queued_suite_without_runs_keeps_the_head_pending(monkeypatch):
    sink = Sink()
    body = {"check_suites": [suite(1), suite(2, status="queued", conclusion=None, runs=0)]}
    monkeypatch.setattr(checks.feed, "gh_get", lambda *_a: GhResponse(200, {}, body))
    aggregator = checks.HeadCheckAggregator(lambda *_a: None, sink, sleep=lambda _n: asyncio.sleep(0))
    aggregator._facts[("o/r", "a")] = (None, None)
    await aggregator._settle(("o/r", "a"))
    assert sink.events == []
