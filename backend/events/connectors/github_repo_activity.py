"""Fallback GitHub repository activity reader, deliberately unwired.

Every emitted item is a webhook-shaped delivery through GitHub admission.  The
issue-event reader is shared by normal polling and startup catch-up so both use
the same bounded cursor traversal and never replay historic labels.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from events.connectors import github_notifications_feed as feed

log = logging.getLogger(__name__)
DISABLE_ENV = "CORSIA_GITHUB_REPO_ACTIVITY_DISABLE"
TEST_ENABLE_ENV = "CORSIA_GITHUB_REPO_ACTIVITY_TEST_ENABLE"
SETTING_PREFIX = "github_repo_activity:"
MAX_HOURLY_READS = 300
WEBHOOK_QUIET = timedelta(minutes=30)
ACTIVE_INTERVAL, IDLE_INTERVAL = 60, 300
_status: dict[str, Any] = {"state": "starting", "reason": None, "counted_reads": 0,
                           "backoff": False, "backoff_until": None, "degraded": None}


def _now() -> datetime:
    return datetime.now(UTC)


def _repo_state(repo: str) -> dict[str, Any]:
    from infra.settings import get_setting
    return get_setting(f"{SETTING_PREFIX}{repo}") or {}


def _save_state(repo: str, state: dict[str, Any]) -> None:
    from infra.settings import set_setting
    set_setting(f"{SETTING_PREFIX}{repo}", state)


def poller_status() -> dict[str, Any]:
    return dict(_status)


def disabled_reason() -> str | None:
    if os.environ.get(DISABLE_ENV) == "1":
        return "config_off"
    if os.environ.get("CORSIA_TEST_MODE") == "1" and os.environ.get(TEST_ENABLE_ENV) != "1":
        return "test_mode"
    return None


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _recent_webhook(repo: str, now: datetime) -> bool:
    from infra.webhook_delivery_log import repo_delivered_since
    return repo_delivered_since(repo, (now - WEBHOOK_QUIET).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _startup_boundary(repo: str, state: dict[str, Any]) -> str | None:
    """Return max(saved event time, latest webhook minus ten minutes), never stored."""
    from infra.webhook_delivery_log import repo_last_delivered_at
    delivered = _parse_time(repo_last_delivered_at(repo))
    webhook_floor = (delivered - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ") if delivered else None
    saved = state.get("issues_cursor_at")
    return max(filter(None, (saved if _parse_time(saved) else None, webhook_floor)), default=None)


def _prune_reads(state: dict[str, Any], now: datetime) -> list[str]:
    floor = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reads = [stamp for stamp in state.get("reads", []) if isinstance(stamp, str) and stamp >= floor]
    state["reads"] = reads
    _status["counted_reads"] = len(reads)
    return reads


def _counted(response: feed.GhResponse, state: dict[str, Any], now: datetime) -> None:
    reads = _prune_reads(state, now)
    if response.status != 304:
        reads.append(now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    state["reads"] = reads
    _status["counted_reads"] = len(reads)
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining and remaining.isdigit() and int(remaining) < 1000:
        until = now + timedelta(seconds=IDLE_INTERVAL)
        _status.update(backoff=True, backoff_until=until.strftime("%Y-%m-%dT%H:%M:%SZ"))


async def _get(runner: feed.GhRunner, path: str, state: dict[str, Any], now: datetime) -> feed.GhResponse | None:
    if len(_prune_reads(state, now)) >= MAX_HOURLY_READS:
        _status.update(state="degraded", degraded="budget_exhausted")
        return None
    until = _parse_time(_status.get("backoff_until"))
    if until is not None and now < until:
        _status.update(state="degraded", degraded="rate_limited", backoff=True)
        return None
    if until is not None:
        _status.update(backoff=False, backoff_until=None)
    etag = state.get("etags", {}).get(path)
    response = await asyncio.to_thread(feed.gh_get, runner, path, {"If-None-Match": etag} if etag else None)
    if response.headers.get("etag"):
        state.setdefault("etags", {})[path] = response.headers["etag"]
    _counted(response, state, now)
    return response


def _payload(repo: str, action: str, pull: dict[str, Any]) -> dict[str, Any]:
    return {"action": action, "repository": {"full_name": repo}, "pull_request": pull}


async def _admit(event: str, payload: dict[str, Any], key: str) -> None:
    from events.connectors.github_admission import admit_github_delivery_async
    await admit_github_delivery_async(event, payload, f"gh-poll:{event}:{key}", source="repo_activity")


def _rows(body: Any, key: str) -> list[dict[str, Any]]:
    values = body.get(key, []) if isinstance(body, dict) else body if isinstance(body, list) else []
    return [value for value in values if isinstance(value, dict)]


async def _issue_events(runner: feed.GhRunner, repo: str, state: dict[str, Any], now: datetime,
                        *, boundary: str | None = None, persist: bool = True) -> bool:
    """Traverse at most three pages until saved id or temporary time floor.

    Candidates are held until a stop is proven; a lost cursor emits no historic
    events and advances to now.  A successful startup boundary is never saved.
    """
    cursor, candidates, newest = state.get("issues_cursor"), [], None
    reached = cursor is None and boundary is None
    for page in range(1, 4):
        response = await _get(runner, f"repos/{repo}/issues/events?per_page=100&page={page}", state, now)
        if response is None or response.status == 304:
            break
        for row in _rows(response.body, ""):
            event_id = str(row.get("id")) if row.get("id") is not None else None
            if newest is None and event_id is not None:
                newest = row
            if event_id == cursor or (boundary and str(row.get("created_at") or "") <= boundary):
                reached = True
                break
            candidates.append(row)
        if reached:
            break
    if not reached and (cursor or boundary):
        _status.update(state="degraded", degraded="cursor_lost")
        state["issues_cursor_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if newest and newest.get("id") is not None:
            state["issues_cursor"] = str(newest["id"])
        _save_state(repo, state)
        return False
    changed = False
    for row in reversed(candidates):
        issue = row.get("issue")
        if row.get("event") != "labeled" or not isinstance(issue, dict):
            continue
        event = "pull_request" if issue.get("pull_request") else "issues"
        payload = _payload(repo, "labeled", issue) if event == "pull_request" else {
            "action": "labeled", "repository": {"full_name": repo}, "issue": issue}
        await _admit(event, payload, f"labeled:{repo}:{row.get('id')}")
        changed = True
    if persist and newest is not None:
        state["issues_cursor"] = str(newest.get("id"))
        state["issues_cursor_at"] = str(newest.get("created_at") or now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return changed


async def _poll_repo(runner: feed.GhRunner, repo: str, now: datetime) -> bool:
    """Poll one fallback repo; a fresh webhook leaves every saved field unchanged."""
    if _recent_webhook(repo, now):
        return False
    state = _repo_state(repo)
    changed = await _issue_events(runner, repo, state, now)
    open_response = await _get(runner, f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=100", state, now)
    if open_response is None:
        _save_state(repo, state)
        return changed
    previous, current = state.get("heads", {}), {}
    for pull in _rows(open_response.body, ""):
        number, head = pull.get("number"), (pull.get("head") or {}).get("sha")
        if not isinstance(number, int) or not isinstance(head, str):
            continue
        current[str(number)] = head
        old = previous.get(str(number))
        if old is None:
            await _admit("pull_request", _payload(repo, "opened", pull), f"opened:{repo}#{number}")
            changed = True
        elif old != head:
            event = _payload(repo, "synchronize", pull)
            event["before"] = old
            await _admit("pull_request", event, f"synchronize:{repo}#{number}:{head}")
            changed = True
    state["heads"] = current
    closed = await _get(runner, f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=30", state, now)
    if closed is not None:
        seen = state.get("closed", {})
        for pull in _rows(closed.body, ""):
            number, marker = pull.get("number"), str(pull.get("updated_at") or pull.get("closed_at") or "")
            if not isinstance(number, int) or not marker or seen.get(str(number)) == marker:
                continue
            action = "closed" if pull.get("state") == "closed" else "reopened"
            await _admit("pull_request", _payload(repo, action, pull), f"{action}:{repo}#{number}:{marker}")
            seen[str(number)] = marker
            changed = True
        state["closed"] = seen
    pending: set[str] = set()
    for status in ("in_progress", "queued"):
        runs = await _get(runner, f"repos/{repo}/actions/runs?status={status}", state, now)
        pending.update(str(row["head_sha"]) for row in _rows(runs.body if runs else {}, "workflow_runs") if row.get("head_sha"))
    for sha in set(state.get("pending", [])) - pending:
        suites = await _get(runner, f"repos/{repo}/commits/{sha}/check-suites", state, now)
        for suite in _rows(suites.body if suites else {}, "check_suites"):
            if suite.get("status") == "completed":
                await _admit("check_suite", {"action": "completed", "repository": {"full_name": repo}, "check_suite": suite}, f"check_suite:{repo}:{suite.get('id')}")
    state["pending"] = sorted(pending)
    if changed:
        state["changed_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_state(repo, state)
    return changed or bool(pending)


async def _startup_catchup(runner: feed.GhRunner, repo: str, now: datetime) -> bool:
    """Process only the startup overlap, without storing the temporary boundary."""
    state = _repo_state(repo)
    boundary = _startup_boundary(repo, state)
    return await _issue_events(runner, repo, state, now, boundary=boundary, persist=False)


async def poll_once(runner: feed.GhRunner, repositories: frozenset[str] | None = None,
                    now: datetime | None = None) -> int:
    reason = disabled_reason()
    if reason:
        _status.update(state="disabled", reason=reason)
        return IDLE_INTERVAL
    now, repos = now or _now(), repositories if repositories is not None else feed.tracked_github_repositories()
    _status["degraded"] = None
    active = any([await _poll_repo(runner, repo, now) for repo in sorted(repos)])
    active = active or any((stamp := _parse_time(_repo_state(repo).get("changed_at"))) and now - stamp <= timedelta(minutes=15) for repo in repos)
    if _status["degraded"]:
        _status.update(state="degraded", reason=_status["degraded"])
    else:
        _status.update(state="enabled", reason=None)
    return ACTIVE_INTERVAL if active else IDLE_INTERVAL


async def repo_activity_poll_loop(runner: feed.GhRunner | None = None) -> None:
    if runner is None:
        from reviewer.gh_api import run_gh
        runner = run_gh
    repos = feed.tracked_github_repositories()
    if disabled_reason() is None:
        for repo in sorted(repos):
            await _startup_catchup(runner, repo, _now())
    while True:
        await asyncio.sleep(await poll_once(runner, repos))
