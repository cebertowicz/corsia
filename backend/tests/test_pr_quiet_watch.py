"""The quiet-PR sweep wakes an owner once per head per overdue reason
(design §7 tests 4-11, and the §9 reviewer gate).

GitHub is a fake ``gh`` runner that answers ``gh api -i`` with canned bodies
and records every path read; the database is disposable, and a CI event
reaching CORSIA is a check-suite row in its delivery log; wakes are captured
at ``deliver_to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from events import delivery
from events.connectors import github_pr_quiet_reasons as reasons
from events.connectors import github_pr_quiet_rows as rows
from events.connectors import github_pr_quiet_watch as watch
from infra.settings import get_setting, set_setting
from tests.event_routing_helpers import mk_session, register_pr

REPO = "o/r"
H = "a" * 40
H2 = "b" * 40
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class FakeGitHub:
    """A ``gh`` runner: ``pulls/{n}``, workflow runs and statuses by path."""

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {}
        self.reads: list[str] = []
        self.auth_ok = True
        self.workflows(1)

    def pr(self, number: int, head: str = H, *, state: str = "open",
           draft: bool = False, mergeable: bool | None = True) -> None:
        self.bodies[f"repos/{REPO}/pulls/{number}"] = {
            "state": state, "head": {"sha": head}, "draft": draft,
            "mergeable": mergeable, "html_url": f"https://github.com/{REPO}/pull/{number}",
        }

    def runs(self, head: str, *runs: dict) -> None:
        """Each run is its own workflow unless it names one; its check suite
        is its run id unless it names one.
        """
        listed = [{"workflow_id": index, "id": index, **run} for index, run in enumerate(runs, 1)]
        self.bodies[f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100"] = {
            "workflow_runs": [{"check_suite_id": run["id"], **run} for run in listed],
        }

    def workflows(self, count: int) -> None:
        self.bodies[f"repos/{REPO}/actions/workflows?per_page=1"] = {"total_count": count}

    def reviewer(self, head: str, state: str) -> None:
        self.bodies[f"repos/{REPO}/commits/{head}/status"] = {
            "statuses": [{"context": "qa/llm-reviewer", "state": state}],
        }

    def __call__(self, args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
        assert timeout > 0, "every gh call is bounded"
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0 if self.auth_ok else 1, "", "")
        path = args[-1]
        self.reads.append(path)
        body = self.bodies.get(path)
        status = "200 OK" if body is not None else "404 Not Found"
        stdout = f"HTTP/2.0 {status}\nEtag: W/\"{len(self.reads)}\"\n\n{json.dumps(body or {})}"
        return subprocess.CompletedProcess(args, 0, stdout, "")


def _z(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def run(conclusion: str | None = "success", *, at: datetime = T0, **fields: Any) -> dict:
    """A run finished at ``at`` whose latest attempt started 5 min before; its
    check suite is its run id unless it names one.
    """
    if "id" in fields:
        fields.setdefault("check_suite_id", fields["id"])
    return {
        "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
        "updated_at": _z(at), "run_started_at": _z(at - timedelta(minutes=5)),
        "run_attempt": 1, **fields,
    }


def _received(head: str, at: datetime = T0, repo: str = REPO, *, suite: int = 1) -> None:
    """Log a completed delivery for check suite ``suite`` of ``head``, recorded at ``at``."""
    from infra.database import write_transaction
    from infra.webhook_delivery_log import record_delivery

    delivery_id = f"d-{head[:6]}-{suite}-{at.isoformat()}"
    record_delivery(
        event_type="check_suite", action="completed", delivery_id=delivery_id, repo=repo,
        payload={"action": "completed", "check_suite": {"id": suite, "head_sha": head}},
    )
    with write_transaction(None, scope="test_delivery_time") as db:
        db.execute("UPDATE webhook_deliveries SET created_at=? WHERE delivery_id=?",
                   (at.strftime("%Y-%m-%dT%H:%M:%SZ"), delivery_id))


@pytest.fixture()
def gh(isolated_db, monkeypatch):
    watch._etags.clear()
    watch._status.update({"fired_count": 0})
    # Gate behavior is covered separately; quiet-watch tests only need its detached handoff.
    monkeypatch.setattr("events.reviewer_gate.evaluate_overdue", lambda *_args: None)
    _received("0" * 40, T0 - timedelta(days=30), suite=0)  # the log has recorded suites for a month
    return FakeGitHub()


@pytest.fixture()
def wakes(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(watch.delivery, "deliver_to_thread", send)
    return send


@pytest.fixture()
def parked(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "events.dead_letter.park", lambda *a, **_k: calls.append(a) or f"dl-{len(calls)}",
    )
    return calls


def _webhook(on: bool = True) -> None:
    from infra import key_store
    from infra.database import write_transaction

    if not on:
        key_store.clear_value("github-webhook")
        return
    key_store.set_value("github-webhook", "hook-value-for-tests")
    with write_transaction(None, scope="test_webhook_time") as db:  # configured a month ago
        db.execute("UPDATE key_store_entries SET set_at=? WHERE name='github-webhook'",
                   ((T0 - timedelta(days=30)).replace(tzinfo=None).isoformat(),))  # naive, as stored


def _logged_without_suite_id(at: datetime) -> None:
    """A check-suite row written before the log recorded suite ids."""
    from infra.database import write_transaction

    with write_transaction(None, scope="test_legacy_delivery") as db:
        db.execute(
            "INSERT INTO webhook_deliveries (id,event_type,action,delivery_id,payload_hash,"
            "status,created_at) VALUES (?,'check_suite','completed',?,'h','dispatched',?)",
            (f"legacy-{at.isoformat()}", f"legacy-{at.isoformat()}", _z(at)),
        )


def _reviewer_runs_here() -> None:
    from infra.database import write_transaction

    with write_transaction(None, scope="test_seed_reviewer") as db:
        db.execute("INSERT INTO reviewer_gate_state VALUES ('o/r#999', 'old')")


def _owner(number: int, tag: str = "owner") -> str:
    thread = mk_session(f"{tag}-{number}")
    register_pr(REPO, number, thread)
    return thread


async def _sweep(gh: FakeGitHub, at: datetime, monkeypatch) -> None:
    monkeypatch.setattr(watch, "_now", lambda: at)
    await watch.sweep_once(gh)


def _reasons(send: AsyncMock) -> list[str]:
    return [call.args[3]["reason"] for call in send.await_args_list]


# -- Test 5: ci_event_missing --


@pytest.mark.asyncio
async def test_ci_event_missing_fires_once_after_ten_minutes(gh, wakes, parked, monkeypatch):
    _webhook()
    owner = _owner(1)
    gh.pr(1)
    gh.runs(H, run("failure"), run("cancelled"))

    await _sweep(gh, T0 + timedelta(minutes=9), monkeypatch)
    assert wakes.await_count == 0
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=16), monkeypatch)
    watch._etags.clear()  # a restart keeps only the settings rows
    await _sweep(gh, T0 + timedelta(minutes=21), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_EVENT_MISSING]
    thread, event, summary, details = wakes.await_args.args
    assert (thread, event) == (owner, "github.pr_events_overdue")
    assert summary.startswith(f"PR o/r#1: CI finished for head {H[:7]} (failure)")
    assert "do not report it yourself" in summary
    assert details == {
        "reason": "ci_event_missing", "head_sha": H, "conclusion": "failure",
        "mergeable": "MERGEABLE", "url": "https://github.com/o/r/pull/1",
    }
    marker = reasons.fired_marker(reasons.CI_EVENT_MISSING, reasons.ci_state(
        {"workflow_runs": [run("failure", id=1, workflow_id=1), run("cancelled", id=2, workflow_id=2)]},
    ))
    assert marker.startswith("ci_event_missing@")
    assert wakes.await_args.kwargs["delivery_id"] == f"pr_quiet:o/r#1:{H}:{marker}"
    assert get_setting("pr_quiet:o/r#1")["fired"] == [marker]


def _failed_wake_setup(number: int, gh: FakeGitHub, monkeypatch) -> tuple[str, str, list]:
    """A PR whose CI event is lost, an owner with a parent, and a spy on real sends."""
    from infra.database import write_transaction

    _webhook()
    owner, parent = _owner(number), mk_session(f"quiet-parent-{number}")
    with write_transaction(None, scope="test_quiet_parent") as db:
        db.execute("UPDATE threads SET parent_thread_id=? WHERE id=?", (parent, owner))
    gh.pr(number)
    gh.runs(H, run("failure"))
    sends: list[str] = []
    real_send = delivery.send_thread_input

    async def spy(thread_id, *args, **kwargs):
        sends.append(kwargs["idempotency_key"])
        return await real_send(thread_id, *args, **kwargs)

    monkeypatch.setattr(delivery, "send_thread_input", spy)
    monkeypatch.setattr("infra.turn_admission_driver.drive_turn_admission", AsyncMock())
    return owner, parent, sends


def _wake_rows(thread: str) -> list[tuple[str, str]]:
    from infra.database import get_db

    return [tuple(row) for row in get_db().execute(
        "SELECT id, delivery_state FROM messages WHERE thread_id=? AND metadata LIKE ?",
        (thread, "%github.pr_events_overdue%"),
    ).fetchall()]


def _failure_reports(thread: str) -> int:
    from infra.database import get_db
    from infra.failed_wake_surface import REPORT_EVENT

    return len(get_db().execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND metadata LIKE ?",
        (thread, f"%{REPORT_EVENT}%"),
    ).fetchall())


@pytest.mark.asyncio
async def test_a_quiet_wake_that_fails_after_acceptance_is_reported_and_never_resent(
    gh, parked, monkeypatch,
):
    from infra.database import write_transaction
    from infra.queued_input_settlement import fail_queued

    owner, parent, sends = _failed_wake_setup(2574, gh, monkeypatch)
    monkeypatch.setattr("automation.self_restart.restart_holds_new_work", lambda **_k: True)
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    [(message_id, state)] = _wake_rows(owner)
    assert state == "queued" and len(sends) == 1
    fired = get_setting("pr_quiet:o/r#2574")["fired"]

    with write_transaction(None, scope="test_quiet_admission_refused") as db:
        queued = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchall()
        fail_queued(db, owner, queued, error="model unavailable at admission")
    for minutes in (16, 21, 26):
        await _sweep(gh, T0 + timedelta(minutes=minutes), monkeypatch)

    assert len(sends) == 1
    assert _wake_rows(owner) == [(message_id, "failed")]
    assert get_setting("pr_quiet:o/r#2574")["fired"] == fired and len(fired) == 1
    assert _failure_reports(parent) == 1


@pytest.mark.asyncio
async def test_a_quiet_wake_refused_after_its_row_committed_is_not_resent(
    gh, parked, monkeypatch,
):
    from plans.errors import PlanError

    owner, parent, sends = _failed_wake_setup(2575, gh, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise PlanError("plan could not be reconciled")

    monkeypatch.setattr("plans.public.snapshot_after_admission", refuse)
    for minutes in (11, 16, 21):
        await _sweep(gh, T0 + timedelta(minutes=minutes), monkeypatch)

    assert len(sends) == 1
    assert [state for _, state in _wake_rows(owner)] == ["failed"]
    assert len(get_setting("pr_quiet:o/r#2575")["fired"]) == 1
    assert _failure_reports(parent) == 1
    assert watch.quiet_watch_status()["fired_count"] == 0


@pytest.mark.asyncio
async def test_a_quiet_wake_refused_before_any_row_is_sent_on_the_next_sweep(
    gh, parked, monkeypatch,
):
    from infra.send_service import SendRejected

    owner, _parent, sends = _failed_wake_setup(2576, gh, monkeypatch)
    real_send = delivery.send_thread_input

    async def refuse(*_args, **_kwargs):
        raise SendRejected(503, "refused before admission")

    monkeypatch.setattr(delivery, "send_thread_input", refuse)
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    assert _wake_rows(owner) == [] and get_setting("pr_quiet:o/r#2576")["fired"] == []

    monkeypatch.setattr(delivery, "send_thread_input", real_send)
    monkeypatch.setattr("automation.self_restart.restart_holds_new_work", lambda **_k: True)
    await _sweep(gh, T0 + timedelta(minutes=16), monkeypatch)

    assert len(sends) == 1 and [state for _, state in _wake_rows(owner)] == ["queued"]
    assert len(get_setting("pr_quiet:o/r#2576")["fired"]) == 1


@pytest.mark.asyncio
async def test_ci_event_missing_is_silent_when_the_log_holds_the_event(gh, wakes, parked,
                                                                      monkeypatch):
    _webhook()
    _owner(2)
    gh.pr(2)
    gh.runs(H, run("success"))
    _received(H)

    await _sweep(gh, T0 + timedelta(hours=2), monkeypatch)
    assert wakes.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_wake_releases_its_claim_for_the_next_sweep(gh, wakes, parked, monkeypatch):
    _webhook()
    _owner(3)
    gh.pr(3)
    gh.runs(H, run("success"))
    wakes.return_value = False
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    assert get_setting("pr_quiet:o/r#3")["fired"] == []

    wakes.return_value = True
    await _sweep(gh, T0 + timedelta(minutes=16), monkeypatch)
    assert _reasons(wakes) == [reasons.CI_EVENT_MISSING] * 2
    [marker] = get_setting("pr_quiet:o/r#3")["fired"]
    assert marker.startswith("ci_event_missing@")


# -- Test 6 and the §9 gate: review_missing --


@pytest.mark.asyncio
async def test_review_missing_fires_after_45_minutes(gh, wakes, parked, monkeypatch):
    _webhook()
    _reviewer_runs_here()
    _owner(4)
    gh.pr(4)
    gh.runs(H, run("success"))
    gh.reviewer(H, "pending")
    _received(H)

    await _sweep(gh, T0 + timedelta(minutes=44), monkeypatch)
    assert wakes.await_count == 0
    await _sweep(gh, T0 + timedelta(minutes=46), monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=51), monkeypatch)

    assert _reasons(wakes) == [reasons.REVIEW_MISSING]
    assert gh.reads.count(f"repos/o/r/commits/{H}/status") == 1, "a fired reason is not re-read"
    summary = wakes.await_args.args[2]
    assert summary == (
        f"PR o/r#4: CI passed for head {H[:7]} 46 min ago, but the reviewer has not "
        "finished. CORSIA has reported it. Do other work; do not poll."
    )
    assert len(parked) == 1, "the pending-reviewer wording promises a report"


@pytest.mark.asyncio
async def test_review_missing_names_a_finished_reviewer_verdict(gh, wakes, parked, monkeypatch):
    _webhook()
    _reviewer_runs_here()
    _owner(5)
    gh.pr(5)
    gh.runs(H, run("success"))
    gh.reviewer(H, "failure")
    _received(H)

    await _sweep(gh, T0 + timedelta(minutes=46), monkeypatch)
    assert wakes.await_args.args[2] == (
        f"PR o/r#5: the reviewer finished head {H[:7]} (failure), but its verdict never "
        "reached you. Run `gh pr checks 5` once and act on the qa/llm-reviewer result."
    )
    assert parked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["draft", "conflicting", "reviewed", "reviewer_never_ran"])
async def test_review_missing_is_silent(gh, wakes, parked, monkeypatch, case):
    from events.reviewer_gate_state import mark_reviewed

    _webhook()
    if case != "reviewer_never_ran":
        _reviewer_runs_here()
    _owner(6)
    gh.pr(6, draft=case == "draft", mergeable=False if case == "conflicting" else True)
    gh.runs(H, run("success"))
    gh.reviewer(H, "pending")
    _received(H)
    if case == "reviewed":
        mark_reviewed("o/r#6", H)

    await _sweep(gh, T0 + timedelta(hours=3), monkeypatch)
    assert wakes.await_count == 0


@pytest.mark.asyncio
async def test_review_missing_counts_from_ready_for_a_draft(gh, wakes, parked, monkeypatch):
    _webhook()
    _reviewer_runs_here()
    _owner(13)
    gh.pr(13, draft=True)
    gh.runs(H, run("success"))
    gh.reviewer(H, "pending")
    _received(H)

    await _sweep(gh, T0 + timedelta(minutes=5), monkeypatch)
    ready = T0 + timedelta(hours=3)
    gh.pr(13)  # marked ready long after CI finished; the reviewer starts now
    await _sweep(gh, ready, monkeypatch)
    await _sweep(gh, ready + timedelta(minutes=44), monkeypatch)
    assert wakes.await_count == 0
    await _sweep(gh, ready + timedelta(minutes=46), monkeypatch)

    assert _reasons(wakes) == [reasons.REVIEW_MISSING]


# -- Test 7: ci_not_started --


@pytest.mark.asyncio
@pytest.mark.parametrize("workflows", [None, 0])
async def test_ci_not_started_is_silent_in_a_repo_without_actions(gh, wakes, parked, monkeypatch,
                                                                  workflows):
    _owner(8)
    gh.pr(8)
    gh.runs(H)
    if workflows is None:  # GitHub answers 404 when Actions is off for the repo
        del gh.bodies[f"repos/{REPO}/actions/workflows?per_page=1"]
    else:
        gh.workflows(workflows)

    await _sweep(gh, T0, monkeypatch)
    await _sweep(gh, T0 + timedelta(hours=2), monkeypatch)

    assert wakes.await_count == 0
    assert watch.quiet_watch_status()["state"] == "enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("mergeable,expected", [
    (False, "because the PR has merge conflicts. Update the branch from its base and push."),
    (True, "Run `gh pr view 7 --json mergeable,statusCheckRollup` once."),
])
async def test_ci_not_started_fires_after_30_minutes(gh, wakes, parked, monkeypatch, mergeable,
                                                     expected):
    _owner(7)
    gh.pr(7, mergeable=mergeable)
    gh.runs(H)

    await _sweep(gh, T0, monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=29), monkeypatch)
    assert wakes.await_count == 0
    await _sweep(gh, T0 + timedelta(minutes=31), monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=36), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_NOT_STARTED]
    summary = wakes.await_args.args[2]
    assert summary.startswith(f"PR o/r#7: no CI has started for head {H[:7]} in 30 min")
    assert expected in summary
    assert wakes.await_args.args[3]["mergeable"] == ("CONFLICTING" if not mergeable else "MERGEABLE")
    assert parked == [], "a missing CI start is not a delivery fault"


@pytest.mark.asyncio
async def test_a_non_ci_integration_does_not_hide_actions_never_starting(
    gh, wakes, parked, monkeypatch,
):
    """#2493 E3 (withdrawn): a deploy preview or bot status on the head is not CI.

    Counting it as a start would silence the one wake for a head whose Actions
    never ran (fork approval, disabled workflow, lost push); a missed stall is
    worse than a spurious wake.
    """
    _owner(44)
    gh.pr(44)
    gh.runs(H)
    gh.bodies[f"repos/{REPO}/commits/{H}/check-suites?per_page=100"] = {
        "check_suites": [{"app": {"slug": "vercel"}, "latest_check_runs_count": 1}],
    }
    gh.bodies[f"repos/{REPO}/commits/{H}/status"] = {
        "statuses": [{"context": "vercel", "state": "success"}],
    }

    await _sweep(gh, T0, monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=31), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_NOT_STARTED]


@pytest.mark.asyncio
async def test_a_new_head_resets_the_reasons(gh, wakes, parked, monkeypatch):
    _owner(8)
    gh.pr(8)
    gh.runs(H)
    await _sweep(gh, T0, monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=31), monkeypatch)
    gh.pr(8, H2)
    gh.runs(H2)
    await _sweep(gh, T0 + timedelta(minutes=32), monkeypatch)
    assert wakes.await_count == 1
    await _sweep(gh, T0 + timedelta(minutes=63), monkeypatch)
    assert [c.args[3]["head_sha"] for c in wakes.await_args_list] == [H, H2]


def test_due_reasons_leave_out_what_already_fired_for_the_head():
    view = reasons.PrView(REPO, 1, H, False, "MERGEABLE", "u")
    facts = reasons.LocalFacts(webhook=True, ci_received_at=T0, log_answers_from=T0,
                               reviewed_sha=None, reviewer_ran=True)
    ci = reasons.CiState(True, T0, "cancelled", (("Backend", 7),), ((7, T0),), "a1")
    later = T0 + timedelta(hours=1)
    assert reasons.due_reasons(view, ci, facts, fired=[], first_seen=T0, now=later) == [
        reasons.CI_CANCELLED,
    ]
    assert reasons.due_reasons(
        view, ci, facts, fired=["ci_cancelled@a1"], first_seen=T0, now=later,
    ) == []
    assert reasons.due_reasons(
        view, ci, facts, fired=["ci_cancelled@a0"], first_seen=T0, now=later,
    ) == [reasons.CI_CANCELLED], "a CI reason fires once per CI attempt"


# -- Test 4 (sweep half): a poller-only instance --


@pytest.mark.asyncio
async def test_no_webhook_turns_off_ci_event_and_review_reasons(gh, wakes, parked, monkeypatch):
    _webhook(False)
    _reviewer_runs_here()
    _owner(10)
    gh.pr(10, H2)
    gh.runs(H2)
    await _sweep(gh, T0, monkeypatch)

    _owner(9)  # its CI finished with nothing in the log
    gh.pr(9)
    gh.runs(H, run("success"))
    gh.reviewer(H, "pending")
    await _sweep(gh, T0 + timedelta(hours=5), monkeypatch)
    assert _reasons(wakes) == [reasons.CI_NOT_STARTED]
    assert wakes.await_args.args[3]["head_sha"] == H2


# -- Test 8: ended and settled PRs cost one read and never wake --


@pytest.mark.asyncio
async def test_ended_pr_reads_only_its_pull_and_drops_its_rows(gh, wakes, parked, monkeypatch):
    """REST reports a merged PR and a closed one alike: ``state: closed``."""
    _webhook()
    _owner(11)
    gh.pr(11, state="closed")
    set_setting("pr_quiet:o/r#11", {"head": H, "first_seen_at": T0.isoformat(), "fired": []})

    await _sweep(gh, T0 + timedelta(days=3), monkeypatch)
    assert gh.reads == ["repos/o/r/pulls/11"]
    assert wakes.await_count == 0
    assert get_setting("pr_quiet:o/r#11") is None


@pytest.mark.asyncio
async def test_settled_head_stays_silent_for_days_on_pulls_reads_alone(gh, wakes, parked,
                                                                       monkeypatch):
    from events.reviewer_gate_state import mark_reviewed

    _webhook()
    _reviewer_runs_here()
    _owner(12)
    gh.pr(12)
    gh.runs(H, run("success"))
    _received(H)
    mark_reviewed("o/r#12", H)

    await _sweep(gh, T0 + timedelta(minutes=20), monkeypatch)
    assert get_setting("pr_quiet:o/r#12")["settled"] == {"draft": False, "conflicting": False}
    gh.reads.clear()
    for day in range(1, 4):
        await _sweep(gh, T0 + timedelta(days=day), monkeypatch)
    assert gh.reads == ["repos/o/r/pulls/12"] * 3
    assert wakes.await_count == 0

    gh.pr(12, draft=True)  # a changed draft flag is re-evaluated
    await _sweep(gh, T0 + timedelta(days=4), monkeypatch)
    assert gh.reads[-1] == f"repos/o/r/actions/runs?head_sha={H}&per_page=100"


@pytest.mark.asyncio
async def test_reads_are_conditional_and_a_304_reuses_the_cached_body(gh, monkeypatch):
    gh.pr(13)
    first = await watch._get(gh, "repos/o/r/pulls/13")

    def not_modified(args, timeout=30.0):
        assert "If-None-Match: W/\"1\"" in args
        return subprocess.CompletedProcess(args, 0, "HTTP/2.0 304 Not Modified\n\n", "")

    assert await watch._get(not_modified, "repos/o/r/pulls/13") == first


# -- Test 9: one health report per 6 h, however many PRs --


@pytest.mark.asyncio
async def test_health_sink_report_is_sent_once_per_six_hours(gh, wakes, parked, monkeypatch):
    _webhook()
    for number in (14, 15, 16):
        _owner(number)
        gh.pr(number)
    gh.runs(H, run("success"))

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    assert wakes.await_count == 3
    assert len(parked) == 1
    event, payload, reason = parked[0]
    assert (event, reason) == ("github.ingress_overdue", "ingress_overdue")
    assert sorted(payload["pull_requests"]) == [
        f"o/r#{n} (ci_event_missing)" for n in (14, 15, 16)
    ]

    for number in (14, 15, 16):
        gh.pr(number, H2)
    gh.runs(H2, run("success", at=T0 + timedelta(hours=1)))
    await _sweep(gh, T0 + timedelta(hours=2), monkeypatch)
    assert wakes.await_count == 6 and len(parked) == 1

    h3 = "c" * 40
    for number in (14, 15, 16):
        gh.pr(number, h3)
    gh.runs(h3, run("success", at=T0 + timedelta(hours=6)))
    await _sweep(gh, T0 + timedelta(hours=7), monkeypatch)
    assert wakes.await_count == 9 and len(parked) == 2


@pytest.mark.asyncio
async def test_a_fault_inside_the_window_is_in_the_next_report(gh, wakes, parked, monkeypatch):
    """#2493 E1: its wake said CORSIA reported the fault, so the next report names it."""
    _webhook()
    _owner(17)
    gh.pr(17)
    gh.runs(H, run("success"))
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    assert [p[1]["pull_requests"] for p in parked] == [["o/r#17 (ci_event_missing)"]]

    _owner(18)
    gh.pr(18, H2)
    gh.runs(H2, run("success", at=T0 + timedelta(hours=1)))
    await _sweep(gh, T0 + timedelta(hours=2), monkeypatch)
    assert wakes.await_count == 2 and len(parked) == 1, "still one report per 6 h"

    await _sweep(gh, T0 + timedelta(hours=7), monkeypatch)
    assert [p[1]["pull_requests"] for p in parked][1:] == [["o/r#18 (ci_event_missing)"]]
    assert "unreported" not in get_setting(watch.REPORT_SETTING)


@pytest.mark.asyncio
async def test_a_failed_report_is_kept_and_retried(gh, wakes, monkeypatch):
    calls: list[tuple] = []
    results = iter(["", "dl-1"])
    monkeypatch.setattr(
        "events.dead_letter.park", lambda *a, **_k: calls.append(a) or next(results),
    )
    _webhook()
    _owner(20)
    gh.pr(20)
    gh.runs(H, run("success"))

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=16), monkeypatch)

    assert wakes.await_count == 1
    assert [call[1]["pull_requests"] for call in calls] == [["o/r#20 (ci_event_missing)"]] * 2
    assert "unreported" not in get_setting(watch.REPORT_SETTING)


def test_rows_end_with_the_pr_or_its_ownership(gh):
    from infra.database import write_transaction

    owner = _owner(17)
    _owner(18)
    for number in (17, 18, 19):  # 19 has a sweep row but no owner
        set_setting(f"pr_quiet:o/r#{number}", {"head": H})
    set_setting(f"pr_notice:{owner}:o/r#17", {"noticed_at": 1})
    set_setting("pr_notice:formerowner:o/r#18", {"noticed_at": 1})
    set_setting(watch.REPORT_SETTING, {"at": T0.isoformat()})
    with write_transaction(None, scope="test_end_pr") as db:  # the merged webhook came first
        db.execute(
            "UPDATE artifact_identities SET metadata=json_set(COALESCE(metadata, '{}'), "
            "'$.pr_state', 'merged') WHERE external_id='o/r#17'",
        )

    assert rows.prune_rows() == 4
    assert get_setting("pr_quiet:o/r#18")
    assert get_setting(watch.REPORT_SETTING)
    for key in ("pr_quiet:o/r#17", f"pr_notice:{owner}:o/r#17",
                "pr_notice:formerowner:o/r#18", "pr_quiet:o/r#19"):
        assert get_setting(key) is None, key


@pytest.mark.asyncio
async def test_rows_are_pruned_while_the_sweep_is_off(gh, monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.setenv(watch.DISABLE_ENV, "1")
    set_setting("pr_quiet:o/r#21", {"head": H})
    sweep = AsyncMock()
    monkeypatch.setattr(watch, "sweep_once", sweep)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(watch.pr_quiet_watch_loop(gh), 1)

    assert get_setting("pr_quiet:o/r#21") is None
    assert sweep.await_count == 0
    assert watch.quiet_watch_status()["reason"] == "config_off"


# -- Ingress is the delivery log, never when the sweep first saw the PR --


@pytest.mark.asyncio
async def test_first_head_ci_lost_in_an_outage_wakes_and_reports(gh, wakes, parked, monkeypatch):
    """The PR's first head finished CI while CORSIA was down; the sweep first
    sees the PR days later and the log holds no delivery for the head."""
    _webhook()
    _owner(30)
    gh.pr(30)
    gh.runs(H, run("success"))

    await _sweep(gh, T0 + timedelta(days=3), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_EVENT_MISSING]
    assert parked[0][1]["pull_requests"] == ["o/r#30 (ci_event_missing)"]
    assert get_setting("pr_quiet:o/r#30")["settled"] == {"draft": False, "conflicting": False}


@pytest.mark.asyncio
async def test_first_head_review_lost_in_an_outage_wakes_and_reports(gh, wakes, parked,
                                                                     monkeypatch):
    """The CI event arrived, then CORSIA went down before the reviewer finished;
    the sweep first sees the PR days later."""
    _webhook()
    _reviewer_runs_here()
    _owner(31)
    gh.pr(31)
    gh.runs(H, run("success"))
    gh.reviewer(H, "pending")
    _received(H)

    await _sweep(gh, T0 + timedelta(days=3), monkeypatch)

    assert _reasons(wakes) == [reasons.REVIEW_MISSING]
    assert parked[0][1]["pull_requests"] == ["o/r#31 (review_missing)"]


@pytest.mark.asyncio
async def test_ci_received_before_ownership_is_silent_and_settles(gh, wakes, parked, monkeypatch):
    """The PR #1536 shape: CI finished and its event reached CORSIA long before
    anyone owned the PR; owning it now is not a lost event."""
    from events.reviewer_gate_state import mark_reviewed

    _webhook()
    _reviewer_runs_here()
    gh.pr(32)
    gh.runs(H, run("success", at=T0 - timedelta(days=15)))
    _received(H, T0 - timedelta(days=15))
    mark_reviewed("o/r#32", H)
    _owner(32)

    await _sweep(gh, T0, monkeypatch)
    gh.reads.clear()
    await _sweep(gh, T0 + timedelta(hours=1), monkeypatch)

    assert wakes.await_count == 0 and parked == []
    assert gh.reads == ["repos/o/r/pulls/32"], "settled: one pulls read per sweep"


@pytest.mark.asyncio
async def test_ci_before_the_log_could_record_it_is_unknown_not_lost(gh, wakes, parked,
                                                                    monkeypatch):
    """CI that finished before the webhook was configured can be neither
    confirmed nor denied: no wake, no report, and the head settles."""
    _webhook()
    _owner(33)
    gh.pr(33)
    gh.runs(H, run("success", at=T0 - timedelta(days=40)))

    await _sweep(gh, T0, monkeypatch)

    assert wakes.await_count == 0 and parked == []
    assert get_setting("pr_quiet:o/r#33")["settled"] == {"draft": False, "conflicting": False}


@pytest.mark.asyncio
async def test_a_log_that_never_received_a_check_suite_still_judges(gh, wakes, parked,
                                                                   monkeypatch):
    """No pre-column rows and no check suite ever logged (a fresh install, or a
    webhook never subscribed to check_suite): the log can answer, so the CI
    event is lost and ingress is reported. The reviewer is never blamed.
    """
    from infra.database import write_transaction

    with write_transaction(None, scope="test_empty_log") as db:
        db.execute("DELETE FROM webhook_deliveries")
    _webhook()
    _reviewer_runs_here()
    _owner(40)
    gh.pr(40)
    gh.runs(H, run("success"))
    gh.reviewer(H, "pending")

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=50), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_EVENT_MISSING]
    assert [p[0] for p in parked] == ["github.ingress_overdue"]


@pytest.mark.asyncio
async def test_ci_before_the_last_pre_column_row_is_unknown_after_it_lost(gh, wakes, parked,
                                                                         monkeypatch):
    """An upgraded log: pre-column rows, then none since. The floor is the last
    pre-column row, not the first id-bearing one.
    """
    from infra.database import write_transaction

    with write_transaction(None, scope="test_upgraded_log") as db:
        db.execute("DELETE FROM webhook_deliveries")
    _logged_without_suite_id(T0 - timedelta(days=3))
    _logged_without_suite_id(T0 - timedelta(days=1))
    _webhook()
    _owner(41)
    _owner(42)
    gh.pr(41)
    gh.pr(42, H2)
    gh.runs(H, run("success", at=T0 - timedelta(days=2)))
    gh.runs(H2, run("success"))

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)

    assert [c.args[3]["head_sha"] for c in wakes.await_args_list] == [H2]
    assert get_setting("pr_quiet:o/r#41")["settled"] == {"draft": False, "conflicting": False}


@pytest.mark.asyncio
async def test_a_fast_run_logged_on_a_lagging_host_clock_is_received(gh, wakes, parked,
                                                                    monkeypatch):
    """A skipped or seconds-long workflow whose delivery this host stamped just
    before GitHub's ``run_started_at`` (host clock 2 s behind) was received.
    """
    _webhook()
    _owner(43)
    gh.pr(43)
    gh.runs(H, run("skipped", run_started_at=_z(T0)))
    _received(H, T0 - timedelta(seconds=2))

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)

    assert wakes.await_count == 0 and parked == []


# -- Cancelled CI (#2383) --


def test_a_cancelled_run_without_a_successor_is_not_a_pass():
    body = {"workflow_runs": [
        run("success", workflow_id=1, id=10),
        run("cancelled", workflow_id=2, id=11, name="Backend unit tests"),
    ]}
    ci = reasons.ci_state(body)
    assert ci.conclusion == "cancelled"
    assert ci.cancelled == (("Backend unit tests", 11),)
    body["workflow_runs"].append(run("failure", workflow_id=3, id=12))
    assert reasons.ci_state(body).conclusion == "failure", "a real failure outranks a cancel"


def test_a_cancelled_run_superseded_by_a_newer_run_drops_out():
    body = {"workflow_runs": [
        run("success", workflow_id=2, id=12),
        run("cancelled", workflow_id=2, id=11),
        run("success", workflow_id=1, id=10),
    ]}
    ci = reasons.ci_state(body)
    assert (ci.conclusion, ci.cancelled) == ("success", ())
    body["workflow_runs"].append(run(None, workflow_id=2, id=13))
    assert reasons.ci_state(body).completed_at is None, "a rerunning workflow is not finished"


@pytest.mark.asyncio
async def test_cancelled_ci_wakes_to_rerun_and_never_blames_the_reviewer(gh, wakes, parked,
                                                                         monkeypatch):
    """#2383: a job cancelled at its timeout with every other workflow green."""
    _webhook()
    _reviewer_runs_here()
    _owner(34)
    gh.pr(34)
    gh.runs(H, run("success"), run("cancelled", name="Backend unit tests", id=36095294877))
    gh.reviewer(H, "pending")
    _received(H)
    _received(H, suite=36095294877)

    await _sweep(gh, T0 + timedelta(minutes=9), monkeypatch)
    assert wakes.await_count == 0, "a rerun or successor may still come"
    await _sweep(gh, T0 + timedelta(minutes=46), monkeypatch)
    await _sweep(gh, T0 + timedelta(minutes=51), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_CANCELLED]
    assert parked == [], "a cancelled job is not an ingress or reviewer fault"
    summary, details = wakes.await_args.args[2:]
    assert summary == (
        f"PR o/r#34: CI was cancelled for head {H[:7]} 46 min ago and nothing reran it: "
        "Backend unit tests (run 36095294877). A timeout or a manual cancel stopped it, so "
        "this head has no CI result. Rerun it once with `gh run rerun <run id> --failed`, or "
        "fix what hung and push. Then end your turn: CORSIA wakes you when the rerun "
        "finishes. Do not poll."
    )
    assert details["conclusion"] == "cancelled"
    assert get_setting("pr_quiet:o/r#34")["settled"] is None, "a cancelled head keeps its reads"


@pytest.mark.asyncio
async def test_a_rerun_that_is_cancelled_again_wakes_again(gh, wakes, parked, monkeypatch):
    """The wake promises one after the rerun: a second cancel must not go silent."""
    _webhook()
    _owner(36)
    gh.pr(36)
    gh.runs(H, run("cancelled", name="Backend unit tests", id=7))
    _received(H, suite=7)
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    assert _reasons(wakes) == [reasons.CI_CANCELLED]

    gh.runs(H, run(None, name="Backend unit tests", id=7))  # rerun in progress
    await _sweep(gh, T0 + timedelta(minutes=16), monkeypatch)
    later = T0 + timedelta(minutes=35)
    gh.runs(H, run("cancelled", name="Backend unit tests", id=7, at=later, run_attempt=2))
    _received(H, later, suite=7)
    await _sweep(gh, later + timedelta(minutes=11), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_CANCELLED] * 2
    assert len(get_setting("pr_quiet:o/r#36")["fired"]) == 2, "one marker per CI attempt"
    assert parked == []


@pytest.mark.asyncio
async def test_a_lost_rerun_completion_is_lost_not_a_reviewer_fault(gh, wakes, parked,
                                                                   monkeypatch):
    """Reviewer Block at 037c2cc9, case (a): the cancelled attempt's delivery was
    logged, the green rerun's was not. That is a lost CI event, never review_missing.
    """
    _webhook()
    _reviewer_runs_here()
    _owner(37)
    gh.pr(37)
    gh.reviewer(H, "pending")
    gh.runs(H, run("cancelled", id=7))
    _received(H, suite=7)
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)

    later = T0 + timedelta(minutes=60)
    gh.runs(H, run("success", id=7, at=later, run_attempt=2))
    await _sweep(gh, later + timedelta(minutes=1), monkeypatch)
    assert _reasons(wakes) == [reasons.CI_CANCELLED], "not logged yet is not lost yet"
    await _sweep(gh, later + timedelta(minutes=11), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_CANCELLED, reasons.CI_EVENT_MISSING]
    assert [p[0] for p in parked] == ["github.ingress_overdue"]


@pytest.mark.asyncio
async def test_a_failed_head_rerun_green_on_the_same_head_arms_the_reviewer(gh, wakes, parked,
                                                                           monkeypatch):
    """#2493 E2: a red head must not settle, or its green rerun is never read."""
    _webhook()
    _reviewer_runs_here()
    _owner(43)
    gh.pr(43)
    gh.reviewer(H, "pending")
    gh.runs(H, run("failure", id=7))
    _received(H, suite=7)
    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    assert wakes.await_count == 0

    later = T0 + timedelta(minutes=60)
    gh.runs(H, run("success", id=7, at=later, run_attempt=2))
    _received(H, later, suite=7)
    await _sweep(gh, later + timedelta(minutes=44), monkeypatch)
    assert wakes.await_count == 0
    await _sweep(gh, later + timedelta(minutes=46), monkeypatch)

    assert _reasons(wakes) == [reasons.REVIEW_MISSING]


@pytest.mark.asyncio
async def test_the_last_workflow_lost_is_lost_not_a_reviewer_fault(gh, wakes, parked,
                                                                  monkeypatch):
    """Reviewer Block at 037c2cc9, case (b): workflow A's delivery was logged and
    the last workflow B's was lost. The reviewer clock never starts from A.
    """
    _webhook()
    _reviewer_runs_here()
    _owner(38)
    gh.pr(38)
    gh.reviewer(H, "pending")
    gh.runs(H, run("success", id=1, at=T0 - timedelta(minutes=20)), run("success", id=2))
    _received(H, T0 - timedelta(minutes=20), suite=1)

    await _sweep(gh, T0 + timedelta(minutes=46), monkeypatch)

    assert _reasons(wakes) == [reasons.CI_EVENT_MISSING]


@pytest.mark.asyncio
async def test_the_review_clock_starts_at_the_last_ci_delivery(gh, wakes, parked, monkeypatch):
    _webhook()
    _reviewer_runs_here()
    _owner(39)
    gh.pr(39)
    gh.reviewer(H, "pending")
    gh.runs(H, run("success", id=1, at=T0 - timedelta(minutes=30)), run("success", id=2))
    _received(H, T0 - timedelta(minutes=30), suite=1)
    _received(H, T0, suite=2)

    await _sweep(gh, T0 + timedelta(minutes=44), monkeypatch)
    assert wakes.await_count == 0, "45 min from the last delivery, not the first"
    await _sweep(gh, T0 + timedelta(minutes=46), monkeypatch)
    assert _reasons(wakes) == [reasons.REVIEW_MISSING]


def test_runs_of_one_workflow_from_two_triggers_both_count():
    """Reviewer Flag at 037c2cc9: a red ``push`` run is not hidden by a newer
    green ``pull_request`` run of the same workflow on the same head.
    """
    body = {"workflow_runs": [
        run("failure", workflow_id=1, id=10, event="push"),
        run("success", workflow_id=1, id=11, event="pull_request"),
    ]}
    assert reasons.ci_state(body).conclusion == "failure"
    body["workflow_runs"].append(run("success", workflow_id=1, id=12, event="push"))
    assert reasons.ci_state(body).conclusion == "success", "a same-trigger run supersedes"


@pytest.mark.asyncio
async def test_cancelled_then_rerun_green_reads_green(gh, wakes, parked, monkeypatch):
    _webhook()
    _reviewer_runs_here()
    _owner(35)
    gh.pr(35)
    gh.runs(H, run("success"), run("cancelled", workflow_id=2, id=5),
            run("success", workflow_id=2, id=6))
    gh.reviewer(H, "pending")
    _received(H)
    _received(H, suite=6)

    await _sweep(gh, T0 + timedelta(minutes=46), monkeypatch)

    assert _reasons(wakes) == [reasons.REVIEW_MISSING]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [reasons.CI_EVENT_MISSING, reasons.REVIEW_MISSING])
async def test_missing_event_dispatches_one_non_blocking_canonical_gate(
    reason, gh, wakes, parked, monkeypatch,
):
    _webhook()
    _owner(50 if reason == reasons.CI_EVENT_MISSING else 51)
    number = 50 if reason == reasons.CI_EVENT_MISSING else 51
    gh.pr(number)
    if reason == reasons.CI_EVENT_MISSING:
        gh.runs(H, run("failure"))
        due_at = T0 + timedelta(minutes=11)
    else:
        _reviewer_runs_here()
        gh.runs(H, run("success"))
        gh.reviewer(H, "pending")
        _received(H)
        due_at = T0 + timedelta(minutes=46)

    started, release = Event(), Event()
    calls: list[tuple[str, str, int, asyncio.AbstractEventLoop]] = []

    def blocked_gate(repo, head_sha, pr_number, loop):
        calls.append((repo, head_sha, pr_number, loop))
        started.set()
        release.wait(5)

    monkeypatch.setattr("events.reviewer_gate.evaluate_overdue", blocked_gate)
    await _sweep(gh, due_at, monkeypatch)
    assert await asyncio.to_thread(started.wait, 1), "the canonical handoff was not scheduled"
    assert len(calls) == 1 and calls[0][:3] == (REPO, H, number)
    assert parked, "the sweep completed its ingress report while the gate was blocked"

    await _sweep(gh, due_at + timedelta(minutes=5), monkeypatch)
    assert len(calls) == 1, "the durable fired marker must suppress a second handoff"

    release.set()
    await asyncio.gather(*list(watch._OVERDUE_GATE_TASKS))


@pytest.mark.asyncio
async def test_neighbouring_overdue_reason_does_not_dispatch_reviewer_gate(gh, wakes, parked, monkeypatch):
    _webhook()
    _owner(52)
    gh.pr(52)
    gh.runs(H, run("cancelled"))
    _received(H)
    unexpected = Mock(side_effect=AssertionError("cancelled CI must not invoke reviewer gate"))
    monkeypatch.setattr("events.reviewer_gate.evaluate_overdue", unexpected)

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)

    unexpected.assert_not_called()
    assert _reasons(wakes) == [reasons.CI_CANCELLED]


@pytest.mark.asyncio
async def test_committed_refused_owner_wake_does_not_prevent_gate_handoff(
    gh, wakes, parked, monkeypatch,
):
    _webhook()
    _owner(53)
    gh.pr(53)
    gh.runs(H, run("failure"))
    wakes.return_value = False
    monkeypatch.setattr(watch.rows, "wake_committed", lambda _message_id: True)
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        "events.reviewer_gate.evaluate_overdue",
        lambda repo, head_sha, pr_number, _loop: calls.append((repo, head_sha, pr_number)),
    )

    await _sweep(gh, T0 + timedelta(minutes=11), monkeypatch)
    await asyncio.gather(*list(watch._OVERDUE_GATE_TASKS))

    assert wakes.await_count == 1
    assert calls == [(REPO, H, 53)]


@pytest.mark.asyncio
async def test_overdue_gate_preserves_the_canonical_already_reviewed_noop(isolated_db, monkeypatch):
    from events import reviewer_gate, reviewer_gate_state

    reviewer_gate_state.mark_reviewed(reviewer_gate_state.pr_key(REPO, 54), H)
    checks_green = Mock()
    monkeypatch.setattr(reviewer_gate.github_reviewer_gate, "checks_green", checks_green)

    reviewer_gate.evaluate_overdue(REPO, H, 54, asyncio.get_running_loop())

    checks_green.assert_not_called()


# -- Test 10: the wake never touches plans --


@pytest.mark.asyncio
async def test_wake_resolves_no_plan_watch_and_calls_no_plans_code(gh, parked, monkeypatch):
    import events.delivery as delivery_mod

    _webhook()
    owner = _owner(17)
    gh.pr(17)
    gh.runs(H, run("success"))
    send = AsyncMock()
    monkeypatch.setattr(delivery_mod, "send_thread_input", send)
    resolve = AsyncMock(side_effect=AssertionError("the wake resolved a plan watch"))
    monkeypatch.setattr(delivery_mod, "deliver_and_resolve_to_thread", resolve)
    monkeypatch.setattr("plans.watch_resolution.resolve_delivered_event",
                        AsyncMock(side_effect=AssertionError("plan watch resolved")))
    monkeypatch.setattr(watch, "_now", lambda: T0 + timedelta(minutes=11))

    async def inline(fn, *args, **kwargs):  # keep every call on the profiled thread
        return fn(*args, **kwargs)

    monkeypatch.setattr(watch, "run_in_db", inline)
    plan_calls: list[str] = []

    def profile(frame, event, _arg):
        # Generic delivery may import a plans module; running its body is not a call.
        module = frame.f_globals.get("__name__", "")
        if (event == "call" and frame.f_code.co_name != "<module>"
                and (module == "plans" or module.startswith("plans."))):
            plan_calls.append(f"{module}.{frame.f_code.co_name}")

    sys.setprofile(profile)
    try:
        await watch.sweep_once(gh)
    finally:
        sys.setprofile(None)

    assert send.await_count == 1 and send.await_args.args[0] == owner
    assert "[EVENT: github.pr_events_overdue]" in send.await_args.args[2]
    assert resolve.await_count == 0
    assert plan_calls == []


# -- Test 11: off under test mode; shutdown cancels the running loop --


@pytest.mark.asyncio
async def test_loop_is_off_under_test_mode(gh, monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    sweep = AsyncMock()
    monkeypatch.setattr(watch, "sweep_once", sweep)

    await asyncio.wait_for(watch.pr_quiet_watch_loop(gh), 5)

    assert watch.quiet_watch_status()["state"] == "disabled"
    assert watch.quiet_watch_status()["reason"] == "test_mode"
    assert sweep.await_count == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_the_running_loop(gh, monkeypatch):
    import logging

    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from app_startup_runtime import RUNTIME_LOOP_TASKS
    from infra import state

    assert ("pr quiet watch", "_pr_quiet_watch_task") in RUNTIME_LOOP_TASKS
    # Shutdown joins every owned task on ``state``; a session-scoped app
    # started by an earlier test leaves its own there, on another loop.
    for name in (
        "_github_ownership_login_task",
        *(attr for _label, attr in RUNTIME_LOOP_TASKS),
    ):
        monkeypatch.setattr(state, name, None, raising=False)
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.delenv(watch.DISABLE_ENV, raising=False)
    swept = asyncio.Event()

    async def sweep(_runner):
        swept.set()

    monkeypatch.setattr(watch, "sweep_once", sweep)
    task = asyncio.create_task(watch.pr_quiet_watch_loop(gh))
    monkeypatch.setattr(state, "_pr_quiet_watch_task", task, raising=False)
    await asyncio.wait_for(swept.wait(), 5)

    await shutdown_owned_tasks([None] * 5, logging.getLogger("test"))

    assert task.cancelled()


def test_signed_out_gh_disables_the_sweep(gh, monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.delenv(watch.DISABLE_ENV, raising=False)
    gh.auth_ok = False
    assert watch.disabled_reason(gh) == "gh_signed_out"
    monkeypatch.setenv(watch.DISABLE_ENV, "1")
    assert watch.disabled_reason(gh) == "config_off"
