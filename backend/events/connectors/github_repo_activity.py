"""Fallback GitHub repository activity reader, deliberately unwired.

Every emitted item is a webhook-shaped delivery through GitHub admission.  The
issue-event reader is shared by normal polling and startup catch-up so both use
the same bounded cursor traversal and never replay historic labels.  A repo's
first poll only seeds its cursor, heads and closed markers.  Every read passes
one budget and backoff gate.  The poller is a fallback, with one rule for every
read: an item whose own time is at or before the repo's last real webhook
delivery is assumed delivered by it and is not admitted.  Saved state still
moves past a skipped item, so it is never offered again.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from events.connectors import github_issue_activity as issue_activity
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
# One writer per repo state: two polls of one repo never interleave a save.
_locks: dict[str, asyncio.Lock] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _lock(repo: str) -> asyncio.Lock:
    return _locks.setdefault(repo, asyncio.Lock())


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


def _last_delivery(repo: str) -> datetime | None:
    """The latest real webhook delivery for the repo: the one delivery-log read of a poll."""
    from infra.webhook_delivery_log import repo_last_delivered_at
    return _parse_time(repo_last_delivered_at(repo))


def _recent_webhook(delivered: datetime | None, now: datetime) -> bool:
    return delivered is not None and delivered >= now - WEBHOOK_QUIET


def _covered(at: Any, delivered: datetime | None) -> bool:
    """Whether an item's own time is at or before the last real delivery (or cannot be read)."""
    if delivered is None:
        return False
    moment = _parse_time(at)
    return moment is None or moment <= delivered


def _startup_boundary(delivered: datetime | None, state: dict[str, Any]) -> str | None:
    """Return max(saved event time, latest webhook minus ten minutes), never stored."""
    webhook_floor = _stamp(delivered - timedelta(minutes=10)) if delivered else None
    saved = state.get("issues_cursor_at")
    return max(filter(None, (saved if _parse_time(saved) else None, webhook_floor)), default=None)


def _prune_reads(state: dict[str, Any], now: datetime) -> list[str]:
    floor = _stamp(now - timedelta(hours=1))
    reads = [stamp for stamp in state.get("reads", []) if isinstance(stamp, str) and stamp >= floor]
    state["reads"] = reads
    _status["counted_reads"] = len(reads)
    return reads


def _counted(response: feed.GhResponse, state: dict[str, Any], now: datetime) -> None:
    reads = _prune_reads(state, now)
    if response.status != 304:
        reads.append(_stamp(now))
    state["reads"] = reads
    _status["counted_reads"] = len(reads)
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining and remaining.isdigit() and int(remaining) < 1000:
        _status.update(backoff=True, backoff_until=_stamp(now + timedelta(seconds=IDLE_INTERVAL)))


async def _get(runner: feed.GhRunner, path: str, state: dict[str, Any], now: datetime, *,
               send_etag: bool = True, keep_etag: bool = True) -> feed.GhResponse | None:
    """The one gated read: None when the hourly budget, a rate backoff or a failed read stops it."""
    if len(_prune_reads(state, now)) >= MAX_HOURLY_READS:
        _status.update(state="degraded", degraded="budget_exhausted")
        return None
    until = _parse_time(_status.get("backoff_until"))
    if until is not None and now < until:
        _status.update(state="degraded", degraded="rate_limited", backoff=True)
        return None
    if until is not None:
        _status.update(backoff=False, backoff_until=None)
    etag = state.get("etags", {}).get(path) if send_etag else None
    try:
        response = await asyncio.to_thread(feed.gh_get, runner, path, {"If-None-Match": etag} if etag else None)
    except feed.FeedError as exc:
        # A refused or failed read holds the cursor like a budget stop; the next cycle retries.
        _status.update(state="degraded", degraded=exc.error_class)
        return None
    if keep_etag and response.headers.get("etag"):
        state.setdefault("etags", {})[path] = response.headers["etag"]
    _counted(response, state, now)
    return response


def _payload(repo: str, action: str, pull: dict[str, Any]) -> dict[str, Any]:
    return {"action": action, "repository": {"full_name": repo}, "pull_request": pull}


async def _admit(event: str, payload: dict[str, Any], key: str) -> None:
    from events.connectors.github_admission import admit_github_delivery_async
    from infra.webhook_delivery_log import POLL_DELIVERY_PREFIX
    await admit_github_delivery_async(event, payload, f"{POLL_DELIVERY_PREFIX}{event}:{key}", source="repo_activity")


def _rows(body: Any, key: str) -> list[dict[str, Any]]:
    values = body.get(key, []) if isinstance(body, dict) else body if isinstance(body, list) else []
    return [value for value in values if isinstance(value, dict)]


def _commit_cursor(state: dict[str, Any], newest: dict[str, Any] | None, now: datetime,
                   path: str, etag: str | None) -> None:
    """Advance the issue cursor; page one's ETag is kept only with the cursor it describes."""
    if newest is not None:
        state["issues_cursor"] = str(newest["id"])
        state["issues_cursor_at"] = str(newest.get("created_at") or _stamp(now))
    else:
        state.setdefault("issues_cursor_at", _stamp(now))
    if etag:
        state.setdefault("etags", {})[path] = etag


async def _issue_events(runner: feed.GhRunner, repo: str, state: dict[str, Any], now: datetime,
                        *, boundary: str | None = None, persist: bool = True,
                        delivered: datetime | None = None) -> bool:
    """Traverse at most three pages until the saved id or a time floor.

    With no cursor and no floor, a poll only seeds the cursor and catch-up does
    nothing.  Candidates are held until a stop is proven; a 304, a non-200 or a
    budget/backoff stop leaves every cursor field as it was.  A candidate the
    last real webhook delivery covers is skipped, not admitted.  Only three
    complete pages without a stop mark the cursor lost, which emits no history
    and advances to now.  A startup boundary is never saved.
    """
    cursor = state.get("issues_cursor")
    floor = boundary if boundary is not None else (None if cursor else state.get("issues_cursor_at"))
    first = f"repos/{repo}/issues/events?per_page=100&page=1"
    if cursor is None and floor is None:
        if not persist:
            return False
        response = await _get(runner, first, state, now, keep_etag=False)
        if response is not None and response.status == 200:
            seed = next((row for row in _rows(response.body, "") if row.get("id") is not None), None)
            _commit_cursor(state, seed, now, first, response.headers.get("etag"))
        return False
    candidates, newest, first_etag, reached = [], None, None, False
    for page in range(1, 4):
        response = await _get(runner, f"repos/{repo}/issues/events?per_page=100&page={page}", state, now,
                              send_etag=page == 1, keep_etag=False)
        if response is None or response.status != 200:
            return False
        if page == 1:
            first_etag = response.headers.get("etag")
        rows = _rows(response.body, "")
        for row in rows:
            event_id = str(row.get("id")) if row.get("id") is not None else None
            if newest is None and event_id is not None:
                newest = row
            if event_id == cursor or (floor and str(row.get("created_at") or "") <= floor):
                reached = True
                break
            candidates.append(row)
        # A short page ends the list, so every event above a time floor has been seen.
        reached = reached or bool(floor and len(rows) < 100)
        if reached:
            break
    if not reached:
        _status.update(state="degraded", degraded="cursor_lost")
        if newest is None:
            state.pop("issues_cursor", None)
        _commit_cursor(state, newest, now, first, first_etag)
        state["issues_cursor_at"] = _stamp(now)
        _save_state(repo, state)
        return False
    changed = False
    for row in reversed(candidates):
        if _covered(row.get("created_at"), delivered):
            continue  # the webhook owns it; the cursor still moves past it
        delivery = issue_activity.row_delivery(repo, row, state.get("heads", {}))
        if delivery is not None:
            await _admit(*delivery)
            changed = True
    if persist:
        _commit_cursor(state, newest, now, first, first_etag)
    return changed


async def _open_pulls(runner: feed.GhRunner, repo: str, state: dict[str, Any], now: datetime,
                      delivered: datetime | None = None) -> bool | None:
    """Recognize new/reopened PRs from saved heads and closure markers."""
    response = await _get(runner, f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=100", state, now)
    if response is None:
        return None
    if response.status != 200:  # 304: the saved heads still describe the open set
        return False
    seeding, previous, current, changed = "heads" not in state, state.get("heads", {}), {}, False
    for pull in _rows(response.body, ""):
        number, head = pull.get("number"), (pull.get("head") or {}).get("sha")
        if not isinstance(number, int) or not isinstance(head, str):
            continue
        current[str(number)] = head
        old = previous.get(str(number))
        if seeding or old == head or _covered(pull.get("updated_at"), delivered):
            continue
        if old is None:
            closure = state.get("closed", {}).get(str(number))
            action = "reopened" if closure is not None else "opened"
            key = f"{action}:{repo}#{number}" + (f":{closure}" if closure is not None else "")
            await _admit("pull_request", _payload(repo, action, pull), key)
        else:
            event = _payload(repo, "synchronize", pull)
            event["before"] = old
            await _admit("pull_request", event, f"synchronize:{repo}#{number}:{head}")
        changed = True
    state["heads"] = current
    return changed


async def _closed_pulls(runner: feed.GhRunner, repo: str, state: dict[str, Any], now: datetime,
                        delivered: datetime | None = None) -> bool | None:
    response = await _get(runner, f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=30", state, now)
    if response is None:
        return None
    if response.status != 200:
        return False
    seeding, seen, changed = "closed" not in state, dict(state.get("closed", {})), False
    for pull in _rows(response.body, ""):
        number, marker = pull.get("number"), str(pull.get("closed_at") or "")
        if not isinstance(number, int) or not marker or seen.get(str(number)) == marker:
            continue
        seen[str(number)] = marker
        if seeding or _covered(marker, delivered):
            continue
        await _admit("pull_request", _payload(repo, "closed", pull), f"closed:{repo}#{number}:{marker}")
        changed = True
    state["closed"] = seen
    return changed


async def _new_issues(runner: feed.GhRunner, repo: str, state: dict[str, Any], now: datetime,
                      delivered: datetime | None = None) -> bool | None:
    async def get(path: str, saved: dict[str, Any], at: datetime) -> feed.GhResponse | None:
        return await _get(runner, path, saved, at)
    return await issue_activity.new_issues(get, _admit, repo, state, now,
                                           covered=lambda at: _covered(at, delivered))


async def _pending_heads(runner: feed.GhRunner, repo: str, state: dict[str, Any], now: datetime,
                         delivered: datetime | None = None) -> None:
    """Admit completed suites for heads that left the pending set.

    Any incomplete run read keeps the saved pending set and reads no suites; a
    head whose suite read the gate stops stays pending for the next poll.
    """
    cached, pending = state.get("runs", {}), set()
    for status in ("in_progress", "queued"):
        response = await _get(runner, f"repos/{repo}/actions/runs?status={status}", state, now)
        if response is None or response.status not in (200, 304) or (response.status == 304 and status not in cached):
            return
        if response.status == 200:
            cached[status] = sorted({str(row["head_sha"]) for row in _rows(response.body, "workflow_runs")
                                     if row.get("head_sha")})
            state["runs"] = cached
        pending.update(cached[status])
    settled = sorted(set(state.get("pending", [])) - pending)
    for index, sha in enumerate(settled):
        suites = await _get(runner, f"repos/{repo}/commits/{sha}/check-suites", state, now,
                            send_etag=False, keep_etag=False)
        if suites is None:
            pending.update(settled[index:])
            break
        for suite in _rows(suites.body, "check_suites"):
            if suite.get("status") == "completed" and not _covered(suite.get("updated_at"), delivered):
                await _admit("check_suite", {"action": "completed", "repository": {"full_name": repo}, "check_suite": suite},
                             f"check_suite:{repo}:{suite.get('id')}:{suite.get('updated_at')}")
    state["pending"] = sorted(pending)


async def _poll_repo(runner: feed.GhRunner, repo: str, now: datetime) -> bool:
    """Poll one fallback repo; a fresh webhook leaves every saved field unchanged."""
    delivered = _last_delivery(repo)
    if _recent_webhook(delivered, now):
        return False
    async with _lock(repo):
        state = _repo_state(repo)
        changed = await _issue_events(runner, repo, state, now, delivered=delivered)
        for read in (_open_pulls, _closed_pulls, _new_issues):
            result = await read(runner, repo, state, now, delivered)
            if result is None:
                _save_state(repo, state)
                return changed
            changed = changed or result
        await _pending_heads(runner, repo, state, now, delivered)
        if changed:
            state["changed_at"] = _stamp(now)
        _save_state(repo, state)
        return changed or bool(state.get("pending"))


async def _startup_catchup(runner: feed.GhRunner, repo: str, now: datetime) -> bool:
    """Process only the startup overlap, without storing the temporary boundary."""
    async with _lock(repo):
        state = _repo_state(repo)
        delivered = _last_delivery(repo)
        boundary = _startup_boundary(delivered, state)
        return await _issue_events(runner, repo, state, now, boundary=boundary, persist=False,
                                   delivered=delivered)


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
