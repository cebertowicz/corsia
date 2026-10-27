from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime

import pytest

from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse


@pytest.fixture(autouse=True)
def _memory_state(monkeypatch):
    """Repo state lives in memory, the gate starts open and no webhook has ever been delivered."""
    from infra import webhook_delivery_log
    monkeypatch.setattr(webhook_delivery_log, "repo_last_delivered_at", lambda _repo: None)
    stored = {}
    monkeypatch.setattr(poller, "_repo_state", lambda repo: stored.setdefault(repo, {}))
    monkeypatch.setattr(poller, "_save_state", lambda repo, state: stored.__setitem__(repo, state))
    monkeypatch.setattr(poller, "_status", {"state": "starting", "reason": None, "counted_reads": 0,
                                            "degraded": None})
    monkeypatch.setattr(poller, "_locks", {})
    return stored


def suite(identifier, status="completed", conclusion="success", updated_at=None, runs=1):
    # GET commits/{sha}/check-suites shape: no run_attempt, no workflow_run.
    return {"id": identifier, "head_sha": "a", "status": status, "conclusion": conclusion,
            "updated_at": updated_at or f"2026-01-01T00:00:0{identifier}Z", "pull_requests": [],
            "app": {"id": 15368, "slug": "github-actions"}, "latest_check_runs_count": runs}


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


def test_startup_boundary_uses_webhook_overlap():
    delivered = datetime(2026, 1, 3, 2, tzinfo=UTC)
    assert poller._startup_boundary(delivered, {"issues_cursor_at": "2026-01-01T00:00:00Z"}) == "2026-01-03T01:50:00Z"


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
    monkeypatch.setattr(poller.feed, "gh_get", lambda *_args, **_kw: calls.append(True) or GhResponse(304, {}, None))
    assert (await poller._get(lambda *_args: None, "x", state, now)).status == 304
    assert calls == [True] and state["reads"] == []


@pytest.mark.asyncio
async def test_every_read_is_low_priority(monkeypatch):
    seen = []
    monkeypatch.setattr(poller.feed, "gh_get", lambda *_a, **kw: seen.append(kw) or GhResponse(304, {}, None))
    await poller._get(lambda *_a: None, "x", {"reads": []}, datetime(2026, 1, 2, tzinfo=UTC))
    assert seen == [{"low_priority": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining,stopped", [(249, True), (250, False), (999, False)])
async def test_reads_stay_out_of_the_shared_budget_reserve_and_hold_the_cursor(remaining, stopped):
    """Through the real shared budget: only the 5% reserve (250 of 5,000) stops this low-priority poller."""
    reset, calls = int(time.time()) + 1800, []

    def runner(args, **_kw):
        calls.append(args[-1])
        head = f"HTTP/2.0 200 OK\r\nx-ratelimit-remaining: {remaining}\r\nx-ratelimit-reset: {reset}"
        return subprocess.CompletedProcess(args, 0, f"{head}\r\n\r\n[]", "")

    now, state = datetime(2026, 1, 2, tzinfo=UTC), {"reads": [], "issues_cursor": "held"}
    assert (await poller._get(runner, "first", state, now)).status == 200  # the response teaches the budget
    second = await poller._get(runner, "second", state, now)
    assert (second is None) is stopped and calls == (["first"] if stopped else ["first", "second"])
    assert state["issues_cursor"] == "held"
    assert poller.poller_status()["degraded"] == ("reserve_held" if stopped else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [poller.feed.FeedError("rate_limited"), poller.feed.GhSignedOut()])
async def test_failed_read_degrades_and_holds_cursor(monkeypatch, error):
    state = {"reads": [], "issues_cursor": "held"}
    monkeypatch.setattr(poller.feed, "gh_get", lambda *_args, **_kw: (_ for _ in ()).throw(error))
    assert await poller._get(lambda *_args: None, "x", state, datetime(2026, 1, 2, tzinfo=UTC)) is None
    assert state == {"reads": [], "issues_cursor": "held"}
    assert poller.poller_status()["degraded"] == error.error_class


@pytest.mark.asyncio
async def test_previous_head_closed_merged_and_dedupe_shapes(monkeypatch):
    state = {"heads": {"3": "old"}, "closed": {}, "pending": [], "reads": []}
    emitted = []
    open_pull = {"number": 3, "head": {"sha": "new"}}
    merged = {"number": 4, "state": "closed", "merged": True, "updated_at": "now", "closed_at": "closed-4", "head": {"sha": "m"}}
    closed = {"number": 5, "state": "closed", "merged": False, "updated_at": "now", "closed_at": "closed-5", "head": {"sha": "c"}}
    answers = [GhResponse(200, {}, []), GhResponse(200, {}, [open_pull]),
               GhResponse(200, {}, [merged, closed]), GhResponse(304, {}, None), GhResponse(200, {}, {"workflow_runs": []}),
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


def _label_row(identifier, created_at):
    return {"id": identifier, "created_at": created_at, "event": "labeled", "issue": {"pull_request": {}}}


@pytest.mark.asyncio
async def test_a_row_the_last_real_delivery_covers_is_skipped_and_the_cursor_still_advances(monkeypatch):
    delivered = datetime(2026, 1, 2, 0, 0, 30, tzinfo=UTC)
    rows = [_label_row("late", "2026-01-02T00:01:00Z"), _label_row("at", "2026-01-02T00:00:30Z"),
            _label_row("early", "2026-01-02T00:00:10Z"), {"id": "old", "created_at": "2026-01-01T00:00:00Z"}]
    state, emitted = {"issues_cursor": "old", "reads": []}, []
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=GhResponse(200, {}, rows)))
    monkeypatch.setattr(poller, "_admit", lambda _event, _payload, key: emitted.append(key) or asyncio.sleep(0))
    assert await poller._issue_events(lambda *_a: None, "o/r", state, datetime(2026, 1, 3, tzinfo=UTC), delivered=delivered)
    assert emitted == ["labeled:o/r:late"] and state["issues_cursor"] == "late"
    rows[:] = [_label_row("covered", "2026-01-02T00:00:20Z"), {"id": "late", "created_at": "2026-01-02T00:01:00Z"}]
    emitted.clear()
    assert not await poller._issue_events(lambda *_a: None, "o/r", state, datetime(2026, 1, 3, tzinfo=UTC),
                                          delivered=delivered)
    assert emitted == [] and state["issues_cursor"] == "covered"


@pytest.mark.asyncio
async def test_with_no_real_delivery_the_row_is_admitted(monkeypatch):
    state, emitted = {"issues_cursor": "old", "reads": []}, []
    rows = [_label_row("new", "2026-01-02T00:00:00Z"), {"id": "old", "created_at": "2026-01-01T00:00:00Z"}]
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=GhResponse(200, {}, rows)))
    monkeypatch.setattr(poller, "_admit", lambda _event, _payload, key: emitted.append(key) or asyncio.sleep(0))
    assert await poller._issue_events(lambda *_a: None, "o/r", state, datetime(2026, 1, 3, tzinfo=UTC))
    assert emitted == ["labeled:o/r:new"]


def test_covered_compares_instants_not_strings():
    stamp = datetime(2026, 1, 2, 0, 0, 30, tzinfo=UTC)
    assert poller._covered("2026-01-02T00:00:30Z", stamp)
    assert poller._covered("2026-01-02T01:00:00+02:00", stamp)  # an earlier instant
    assert not poller._covered("2026-01-02T00:00:31Z", stamp)
    assert not poller._covered("2026-01-01T23:59:59Z", None)


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
    answers = [GhResponse(200, {}, []), GhResponse(200, {}, []), GhResponse(200, {}, []), GhResponse(200, {}, []),
               GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []}),
               GhResponse(200, {}, {"check_suites": [suite(1, "queued"), suite(2)]})]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_args: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_args: None)
    monkeypatch.setattr(poller, "_get", lambda *_args, **_kw: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda event, _payload, key: emitted.append((event, key)) or asyncio.sleep(0))
    await poller._poll_repo(lambda *_args: None, "o/r", datetime.now(UTC))
    assert emitted == [("check_suite", "check_suite:o/r:2:2026-01-01T00:00:02Z")]


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
               GhResponse(200, {}, [{"number": 4, "state": "closed", "updated_at": "t", "closed_at": "closed-4"}]), GhResponse(200, {}, []),
               GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []})]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_a: None)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda e, p, k: emitted.append(k) or asyncio.sleep(0))
    await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert emitted == []
    assert state["issues_cursor"] == "1" and state["heads"] == {"3": "h"} and state["closed"] == {"4": "closed-4"}


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_runs", [0, 1])
async def test_budget_stop_keeps_pending_heads(monkeypatch, complete_runs):
    state = {"pending": ["a"], "heads": {}, "closed": {}, "issues_cursor": "x", "reads": []}
    answers = [GhResponse(304, {}, None), GhResponse(200, {}, []), GhResponse(200, {}, []), GhResponse(200, {}, [])]
    answers += [GhResponse(200, {}, {"workflow_runs": []})] * complete_runs + [None]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_save_state", lambda *_a: None)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    monkeypatch.setattr(poller, "_admit", lambda *_a: asyncio.sleep(0))
    await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert state["pending"] == ["a"] and answers == []  # no suite read after either incomplete run read


# Edges sharing those causes.

def _recording_gh(monkeypatch, answers):
    sent = []
    monkeypatch.setattr(poller.feed, "gh_get", lambda _runner, path, headers=None, **_kw: sent.append((path, headers)) or answers.pop(0))
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
    answers = [GhResponse(304, {}, None)] * 6
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    assert await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert answers == [] and state["heads"] == {"3": "h"} and state["pending"] == ["a"]


@pytest.mark.asyncio
async def test_gate_stopped_suite_read_keeps_the_head_pending(monkeypatch):
    state = {"issues_cursor": "x", "heads": {}, "closed": {}, "pending": ["a", "b"], "reads": []}
    answers = [GhResponse(304, {}, None), GhResponse(200, {}, []), GhResponse(200, {}, []), GhResponse(200, {}, []),
               GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []}), None]
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _r: state)
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=answers.pop(0)))
    await poller._poll_repo(lambda *_a: None, "o/r", datetime.now(UTC))
    assert answers == [] and state["pending"] == ["a", "b"]

