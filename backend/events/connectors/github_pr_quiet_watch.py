"""Wake a PR's owner once when GitHub's record says an expected event is overdue.

A backend loop sweeps open, owned GitHub pull requests every 5 min. For each
it reads ``GET pulls/{n}`` and, until the head is settled, the head's Actions
workflow runs; with the webhook delivery log (``infra.webhook_delivery_log``),
``events.connectors.github_pr_quiet_reasons`` decides which wakes are due. A wake goes
through ``events.delivery.deliver_to_thread``, never the watch-resolving
variant, so it cannot end a plan wait. Every GitHub read runs in a worker
thread with a timeout, conditional on the last ETag.

State is ``settings`` rows only, kept by ``github_pr_quiet_rows``;
``pr_quiet_ingress_report`` rate-limits the health report. It is off under ``CORSIA_TEST_MODE=1``, when ``CORSIA_PR_QUIET_WATCH_DISABLE=1``,
and while ``gh`` is missing or signed out (rechecked). ``GET /api/system``
reads its status.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from events import delivery
from events.connectors import github_pr_quiet_reasons as reasons
from events.connectors import github_pr_quiet_rows as rows
from events.connectors import github_notifications_feed as feed
from infra.db_async import run_in_db
from reviewer.reviewer_categories import STATUS_CHECK_CONTEXT

log = logging.getLogger(__name__)

DISABLE_ENV = "CORSIA_PR_QUIET_WATCH_DISABLE"
POLL_INTERVAL_S = 5 * 60
JITTER_S = 10.0
HEALTH_REPORT_INTERVAL = timedelta(hours=6)
REPORT_SETTING = "pr_quiet_ingress_report"
_ETAG_CACHE_MAX = 512

_status: dict[str, Any] = {
    "state": "starting", "reason": None, "last_run": None, "fired_count": 0, "last_error": None,
}
_etags: dict[str, tuple[str, Any]] = {}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One open pull request and its active owner thread."""

    thread_id: str
    external_id: str
    repo: str
    number: int


def quiet_watch_status() -> dict[str, Any]:
    """State, reason, last completed sweep, wakes accepted, and the last error class.

    ``fired_count`` counts wakes the owner's FIFO accepted, not wakes delivered
    to a Turn; a later admission failure is reported by the wake's settlement.
    """
    return dict(_status)  # the shared read budget is shown with the notifications poller


def _now() -> datetime:
    return datetime.now(UTC)


def disabled_reason(runner: feed.GhRunner) -> str | None:
    """Why the sweep is off right now, in the notifications poller's reason words."""
    if os.environ.get("CORSIA_TEST_MODE") == "1":
        return "test_mode"
    if os.environ.get(DISABLE_ENV) == "1":
        return "config_off"
    try:
        result = runner(["auth", "status"], timeout=30.0)
    except FileNotFoundError:
        return "gh_missing"
    except Exception:  # noqa: BLE001 — an unreadable auth state is "not signed in"
        return "gh_signed_out"
    return None if result.returncode == 0 else "gh_signed_out"


def _candidates() -> list[Candidate]:
    """Owned, not-ended GitHub PRs whose owner thread is live; one owner per PR."""
    from events.connectors.github_pr_notice import OWNED_PR_SQL, parse_pr_external_id
    from infra.database import get_db

    rows = get_db().execute(OWNED_PR_SQL.format(thread_filter="")).fetchall()
    seen: set[str] = set()
    candidates: list[Candidate] = []
    for row in rows:
        parsed = parse_pr_external_id(row["external_id"])
        if parsed is None or row["external_id"] in seen:
            continue
        seen.add(row["external_id"])
        if delivery.thread_exists_and_active(row["parent_thread_id"]):
            candidates.append(Candidate(row["parent_thread_id"], row["external_id"], *parsed))
    return candidates


async def _get(runner: feed.GhRunner, path: str) -> Any:
    """One conditional GitHub read in a worker thread; a 304 reuses the cached body."""
    cached = _etags.get(path)
    headers = {"If-None-Match": cached[0]} if cached else None
    response = await asyncio.to_thread(
        functools.partial(feed.gh_get, runner, path, headers, low_priority=True),
    )
    if response.status == 304 and cached is not None:
        return cached[1]
    etag = response.headers.get("etag")
    if etag:
        if len(_etags) >= _ETAG_CACHE_MAX:
            _etags.clear()
        _etags[path] = (etag, response.body)
    return response.body


def _ci_received_at(ci: reasons.CiState) -> datetime | None:
    """When the log recorded the last of this CI attempt's completions; None
    while any run's suite has no completed delivery since its attempt started.
    """
    from infra import webhook_delivery_log as ledger

    if not ci.suites:
        return None
    received = ledger.check_suites_received_at([
        (suite, (started - reasons.CLOCK_SLACK).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for suite, started in ci.suites
    ])
    times = [reasons.parse_time(received.get(suite)) for suite, _ in ci.suites]
    return None if None in times else max(t for t in times if t is not None)


def _log_answers_from() -> datetime | None:
    """The earliest time a CI event could have been logged with its suite: once
    the webhook was configured, and after the log's last pre-column row. None
    while no webhook is configured.
    """
    from infra.key_store import GITHUB_WEBHOOK, entry_metadata
    from infra.time import utc_storage_iso_to_api
    from infra.webhook_delivery_log import last_check_suite_without_id

    set_at = entry_metadata(GITHUB_WEBHOOK.name)["set_at"]  # naive UTC storage form
    configured = reasons.parse_time(utc_storage_iso_to_api(set_at)) if set_at else None
    if configured is None:
        return None
    legacy = reasons.parse_time(last_check_suite_without_id())
    return max(configured, legacy) if legacy is not None else configured


def _local_facts(
    candidate: Candidate, ci: reasons.CiState, webhook: bool, log_answers_from: datetime | None,
) -> reasons.LocalFacts:
    """The logged CI deliveries for this CI attempt, the reviewed head, and
    whether the reviewer runs here.
    """
    from events.reviewer_gate_state import get_reviewed_sha, pr_key
    from infra.database import get_db

    prefix = f"{candidate.repo}#"
    reviewer_ran = get_db().execute(
        "SELECT 1 FROM reviewer_gate_state WHERE substr(pr_key, 1, ?) = ? LIMIT 1",
        (len(prefix), prefix),
    ).fetchone() is not None
    return reasons.LocalFacts(
        webhook=webhook,
        ci_received_at=_ci_received_at(ci),
        log_answers_from=log_answers_from,
        reviewed_sha=get_reviewed_sha(pr_key(candidate.repo, candidate.number)),
        reviewer_ran=reviewer_ran,
    )


async def _reviewer_state(runner: feed.GhRunner, view: reasons.PrView) -> str | None:
    body = await _get(runner, f"repos/{view.repo}/commits/{view.head}/status")
    statuses = body.get("statuses") if isinstance(body, dict) else None
    return next(
        (str(s.get("state")) for s in statuses or [] if s.get("context") == STATUS_CHECK_CONTEXT),
        None,
    )


async def _repo_runs_actions(runner: feed.GhRunner, repo: str) -> bool:
    """Whether the repo has Actions workflows; without them no CI is expected to start."""
    try:
        body = await _get(runner, f"repos/{repo}/actions/workflows?per_page=1")
    except feed.FeedError as exc:
        if exc.error_class == "http_404":
            return False
        raise
    return isinstance(body, dict) and (body.get("total_count") or 0) > 0


async def _wake(
    candidate: Candidate, view: reasons.PrView, ci: reasons.CiState, reason: str,
    now: datetime, reviewer_state: str | None,
) -> bool:
    """Claim the reason durably, then send; a send refused before any row releases the claim.

    Claiming first means a retry or restart never wakes twice for one reason:
    the delivery key hashes the summary, which changes between sweeps. Once the
    owner's FIFO holds the wake, the claim stays for this head even if the wake
    later fails: that row's settlement reports the failure to the owner chain
    (``infra.failed_wake_surface``), and it is never sent again (report and
    expire, as for refused continuation wakes).
    """
    marker = reasons.fired_marker(reason, ci)
    if not await run_in_db(rows.update_head_state, candidate.external_id, view.head, fired=marker):
        return False
    details = {
        "reason": reason, "head_sha": view.head, "conclusion": ci.conclusion or "none",
        "mergeable": view.mergeable, "url": view.url,
    }
    if reason == reasons.REVIEW_MISSING:
        details["reviewer_state"] = reviewer_state or "pending"
    event = "github.pr_events_overdue"
    summary = reasons.summary(view, reason, ci, now, reviewer_state)
    delivery_id = f"pr_quiet:{candidate.external_id}:{view.head}:{marker}"
    accepted = await delivery.deliver_to_thread(
        candidate.thread_id, event, summary, details, delivery_id=delivery_id,
    )
    message_id = delivery._event_idempotency_key(event, summary, details, delivery_id=delivery_id)
    if accepted:
        _status["fired_count"] += 1
    elif not await run_in_db(rows.wake_committed, message_id):
        await run_in_db(rows.update_head_state, candidate.external_id, view.head, unfired=marker)
    return accepted


async def sweep_candidate(
    runner: feed.GhRunner, candidate: Candidate, *, webhook: bool,
    log_answers_from: datetime | None,
) -> list[str]:
    """Evaluate one PR; return the reasons that now count as a delivery fault."""
    pr = await _get(runner, f"repos/{candidate.repo}/pulls/{candidate.number}")
    head = ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None
    if not isinstance(pr, dict) or pr.get("state") != "open" or not head:
        await run_in_db(rows.forget, candidate.external_id)
        return []
    view = reasons.PrView(
        candidate.repo, candidate.number, head, bool(pr.get("draft")),
        reasons.mergeable_label(pr.get("mergeable")), str(pr.get("html_url") or ""),
    )
    now = _now()
    state = await run_in_db(rows.head_state, candidate.external_id, head, now)
    held = view.draft or view.conflicting
    if bool(state.get("review_hold")) != held:
        state = await run_in_db(rows.record_review_hold, candidate.external_id, head, held, now)
    settled_key = {"draft": view.draft, "conflicting": view.conflicting}
    if state.get("settled") == settled_key:
        return []
    ci = reasons.ci_state(await _get(
        runner, f"repos/{candidate.repo}/actions/runs?head_sha={head}&per_page=100",
    ))
    facts = await run_in_db(_local_facts, candidate, ci, webhook, log_answers_from)
    fired = list(state.get("fired") or [])
    first_seen = reasons.parse_time(state.get("first_seen_at")) or now
    faults: list[str] = []
    review_from = reasons.parse_time(state.get("review_from"))
    for reason in reasons.due_reasons(
        view, ci, facts, fired=fired, first_seen=first_seen, now=now, review_from=review_from,
    ):
        if reason == reasons.CI_NOT_STARTED and not await _repo_runs_actions(runner, view.repo):
            continue
        reviewer_state = (
            await _reviewer_state(runner, view) if reason == reasons.REVIEW_MISSING else None
        )
        if not await _wake(candidate, view, ci, reason, now, reviewer_state):
            continue
        fired.append(reasons.fired_marker(reason, ci))
        # The wake tells the owner CORSIA reported these, so the report must happen.
        if reason == reasons.CI_EVENT_MISSING or (
            reason == reasons.REVIEW_MISSING and not reasons.reviewer_finished(reviewer_state)
        ):
            faults.append(reason)
    if reasons.settled(view, ci, facts, fired):
        await run_in_db(rows.update_head_state, candidate.external_id, head, settled=settled_key)
    return faults


def _report_ingress(affected: list[str], now: datetime) -> None:
    """Park one ``github.ingress_overdue`` report for the workspace health sink.

    The fault is the instance's ingress, so at most one report every 6 h. Faults
    woken inside the window, or from a failed park, are kept for the next report,
    because their wakes already said the fault was reported.
    """
    from events import dead_letter
    from infra.settings import get_setting, set_setting

    row = get_setting(REPORT_SETTING) or {}
    pending = list(dict.fromkeys([*(row.get("unreported") or []), *affected]))
    previous = reasons.parse_time(row.get("at"))
    if previous is not None and now - previous < HEALTH_REPORT_INTERVAL:
        if affected:
            set_setting(REPORT_SETTING, {**row, "unreported": pending})
        return
    if not pending:
        return
    parked = dead_letter.park(
        "github.ingress_overdue",
        {"summary": "GitHub CI or reviewer events are overdue for registered PRs.",
         "pull_requests": pending},
        "ingress_overdue",
    )
    set_setting(REPORT_SETTING, (
        {"at": now.isoformat(), "pull_requests": pending} if parked
        else {**row, "unreported": pending}
    ))


async def sweep_once(runner: feed.GhRunner) -> None:
    """Evaluate every candidate once; one PR's failure never stops the others."""
    from events.connectors.github_pr_notice import webhook_configured

    candidates = await run_in_db(_candidates)
    webhook = await run_in_db(webhook_configured)
    answers_from = await run_in_db(_log_answers_from)
    affected: list[str] = []
    error: str | None = None
    for candidate in candidates:
        try:
            faults = await sweep_candidate(
                runner, candidate, webhook=webhook, log_answers_from=answers_from,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced in status, next PR still swept
            error = getattr(exc, "error_class", type(exc).__name__)
            log.warning("pr-quiet-watch: %s failed: %s", candidate.external_id, error)
            continue
        affected += [f"{candidate.external_id} ({fault})" for fault in faults]
    await run_in_db(_report_ingress, affected, _now())
    _status.update({
        "state": "degraded" if error else "enabled", "reason": error,
        "last_run": _now().isoformat(), "last_error": error or _status["last_error"],
    })


async def pr_quiet_watch_loop(runner: feed.GhRunner | None = None) -> None:
    """Sweep until cancelled; test mode ends the loop.

    While the sweep is off for another reason the loop still prunes rows, since
    the notice and the receipt keep writing them, and it rechecks the reason.
    """
    if runner is None:
        from reviewer.gh_api import run_gh

        runner = run_gh
    while True:
        reason = await asyncio.to_thread(disabled_reason, runner)
        if reason is not None:
            _status.update({"state": "disabled", "reason": reason})
            if reason == "test_mode":
                return
        try:
            await run_in_db(rows.prune_rows)
            if reason is None:
                await sweep_once(runner)
        except Exception as exc:  # noqa: BLE001 — the loop outlives one bad sweep
            log.warning("pr-quiet-watch: sweep failed: %s", type(exc).__name__)
            _status.update({"state": "failing", "reason": type(exc).__name__})
        await asyncio.sleep(POLL_INTERVAL_S + random.uniform(0, JITTER_S))
