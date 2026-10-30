"""Read-only GitHub reads for the notifications poller, and payload building.

Every call is ``gh api --method GET -i``: this module never writes to GitHub
and never marks a notification read. A polled comment becomes a payload shaped
like the matching webhook (``issue_comment`` or
``pull_request_review_comment``, action ``created``). The REST issue, pull
request and comment objects are key supersets of the webhook's, so they are
used as-is; ``sender`` is the comment author, as in a webhook.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from connections.github_read_request import GithubReadRefused
from events.connectors.github_read_budget import budget

GhRunner = Callable[..., subprocess.CompletedProcess]
log = logging.getLogger(__name__)

_API_PREFIX = "https://api.github.com/"
_SUBJECT_URL = re.compile(r"^https://api\.github\.com/repos/([^/]+/[^/]+)/(issues|pulls)/(\d+)$")
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
_SUBJECT_TYPES = frozenset({"PullRequest", "Issue"})
#: Notification reasons that mean the account took part in the thread, out of
#: those GitHub documents. Excluded: ``subscribed`` (a watched repository) and
#: ``team_mention`` (a team's mention), whose comments cannot concern this
#: instance's work, plus reasons whose subjects are never PR or issue comments
#: (``ci_activity``, ``invitation``, ``security_alert`` and the like).
ADMITTED_REASONS = frozenset({
    "assign", "author", "comment", "manual", "mention", "review_requested",
    "state_change",
})
#: Paging bound per read. A read cut short here is reported as truncated, and
#: the poller holds its cursor so nothing past the cut is dropped.
MAX_PAGES = 10
_GH_TIMEOUT_S = 30.0


class FeedError(RuntimeError):
    """A GitHub read failed; ``error_class`` names the failure for status."""

    def __init__(self, error_class: str, detail: str = "") -> None:
        super().__init__(f"{error_class}: {detail}" if detail else error_class)
        self.error_class = error_class


class GhSignedOut(FeedError):
    """GitHub rejected the instance's ``gh`` credentials."""

    def __init__(self) -> None:
        super().__init__("gh_signed_out")


def github_repositories_for_policy(policy: Any) -> set[str]:
    """The GitHub repositories one registered source repository's remotes name."""
    from infra.worktree_reaper_probe import github_remote_full_name

    if not policy.source_binding_valid or policy.enforcement_error:
        return set()
    return {repository for remote in ("origin", "upstream")
            if (repository := github_remote_full_name(policy.path, remote)) is not None}


def _policies(project_id: str | None = None) -> list[Any]:
    from infra.project_repo_policy import RepoPolicyQueryError, list_registered_repo_policies

    try:
        return (list_registered_repo_policies(project_id=project_id) if project_id
                else list_registered_repo_policies())
    except RepoPolicyQueryError as exc:
        raise FeedError("repo_scope_unavailable") from exc


def tracked_github_repositories(project_id: str | None = None) -> frozenset[str]:
    """Resolve tracked GitHub repositories from the registered source repositories,
    of one project when ``project_id`` is given."""
    from infra.worktree_reaper_probe import ReaperGitError

    try:
        return frozenset(repo for policy in _policies(project_id) for repo in github_repositories_for_policy(policy))
    except ReaperGitError as exc:
        raise FeedError("repo_scope_unavailable") from exc


def projects_tracking_github_repository(repository: str) -> frozenset[str]:
    """The inverse: the projects whose registered source repositories track
    ``repository``. Bounded by the registered repositories, never by any rule."""
    from infra.worktree_reaper_probe import ReaperGitError

    wanted = repository.lower()
    try:
        return frozenset(policy.project_id for policy in _policies()
                         if wanted in github_repositories_for_policy(policy))
    except ReaperGitError as exc:
        raise FeedError("repo_scope_unavailable") from exc


@dataclass(frozen=True, slots=True)
class GhResponse:
    status: int
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True, slots=True)
class FeedPage:
    """One notifications read: ``not_modified`` means nothing else was read."""

    not_modified: bool
    threads: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    last_modified: str | None = None
    server_time: datetime | None = None
    poll_interval_s: int | None = None
    dropped_untracked: int = 0


def iso(moment: datetime) -> str:
    """GitHub's ``since`` timestamp form."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stdout: str) -> GhResponse | None:
    text = stdout.replace("\r\n", "\n")
    head, _, raw_body = text.partition("\n\n")
    lines = head.split("\n")
    match = re.match(r"^HTTP/\S+\s+(\d{3})", lines[0]) if lines else None
    if match is None:
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    try:
        body = json.loads(raw_body) if raw_body.strip() else None
    except json.JSONDecodeError:
        body = None
    return GhResponse(int(match.group(1)), headers, body)


def gh_get(
    runner: GhRunner, path: str, headers: dict[str, str] | None = None, *, low_priority: bool = False,
) -> GhResponse:
    """Run one GET through ``gh``; raise ``FeedError`` unless it is 2xx or 304.

    The shared read budget is asked first: while core is exhausted, or under a
    Retry-After, GitHub is not called. A ``low_priority`` read also stays out
    of the budget's reserve. The budget also decides what counts as a rate limit.
    """
    if budget.blocked_until(low_priority=low_priority) is not None:
        raise FeedError("rate_limited", "shared read budget")
    args = ["api", "--method", "GET", "-i"]
    for name, value in (headers or {}).items():
        args += ["-H", f"{name}: {value}"]
    try:
        result = runner([*args, path], timeout=_GH_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise FeedError("gh_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise FeedError("timeout") from exc
    except GithubReadRefused as exc:
        raise FeedError("read_refused", f"{exc.reason} {exc.detail}".strip()) from exc
    response = _parse(result.stdout or "")
    if response is None:
        raise FeedError("gh_error", (result.stderr or "")[:200])
    rate_limited = budget.record(response.status, response.headers, response.body)
    if response.status == 401:
        raise GhSignedOut()
    if rate_limited:
        raise FeedError("rate_limited")
    if response.status != 304 and not 200 <= response.status < 300:
        raise FeedError(f"http_{response.status}")
    return response


def _next_path(response: GhResponse) -> str | None:
    match = _NEXT_LINK.search(response.headers.get("link", ""))
    if match is None:
        return None
    url = match.group(1)
    return url[len(_API_PREFIX):] if url.startswith(_API_PREFIX) else url


def _get_all(runner: GhRunner, path: str) -> tuple[list[dict[str, Any]], bool]:
    """Read up to ``MAX_PAGES`` pages; return the items and whether pages remain."""
    items: list[dict[str, Any]] = []
    next_path: str | None = path
    for _page in range(MAX_PAGES):
        if next_path is None:
            break
        response = gh_get(runner, next_path)
        if not isinstance(response.body, list):
            raise FeedError("invalid_body", next_path)
        items.extend(item for item in response.body if isinstance(item, dict))
        next_path = _next_path(response)
    return items, next_path is not None


def list_notifications(
    runner: GhRunner, since: datetime, last_modified: str | None,
) -> FeedPage:
    """Read updated PR and issue threads, conditionally on ``Last-Modified``."""
    path = f"notifications?all=true&per_page=50&since={iso(since)}"
    conditional = {"If-Modified-Since": last_modified} if last_modified else None
    first = gh_get(runner, path, conditional)
    interval = first.headers.get("x-poll-interval")
    server_date = first.headers.get("date")
    page = FeedPage(
        not_modified=first.status == 304,
        last_modified=first.headers.get("last-modified") or last_modified,
        server_time=parsedate_to_datetime(server_date) if server_date else None,
        poll_interval_s=int(interval) if interval and interval.isdigit() else None,
    )
    if page.not_modified:
        return page
    if not isinstance(first.body, list):
        raise FeedError("invalid_body", "notifications")
    threads = [item for item in first.body if isinstance(item, dict)]
    truncated = False
    next_path = _next_path(first)
    if next_path is not None:
        more, truncated = _get_all(runner, next_path)
        threads.extend(more)
    tracked = tracked_github_repositories()
    admitted_threads = []
    dropped_untracked = 0
    for thread in threads:
        subject = thread.get("subject") or {}
        match = _SUBJECT_URL.match(str(subject.get("url") or ""))
        if subject.get("type") not in _SUBJECT_TYPES or thread.get("reason") not in ADMITTED_REASONS:
            continue
        repository = match.group(1).lower() if match is not None else None
        if repository not in tracked:
            dropped_untracked += 1
            log.info("github-notifications: dropping untracked repository notification (%s)", repository)
            continue
        admitted_threads.append(thread)
    return replace(
        page,
        truncated=truncated,
        threads=admitted_threads,
        dropped_untracked=dropped_untracked,
    )


def _created_since(comment: dict[str, Any], since: datetime) -> bool:
    created = comment.get("created_at")
    if not isinstance(created, str):
        return False
    return datetime.fromisoformat(created.replace("Z", "+00:00")) >= since


def comment_payloads(
    runner: GhRunner,
    thread: dict[str, Any],
    since: datetime,
    already_recorded: Callable[[str], bool],
) -> list[tuple[str, dict[str, Any]]]:
    """Return webhook-shaped payloads for comments created since ``since``."""
    match = _SUBJECT_URL.match(str((thread.get("subject") or {}).get("url") or ""))
    if match is None:
        return []
    repo, kind, number = match.groups()
    repository = thread.get("repository") or {"full_name": repo}
    sources = [("issue_comment", "issue", f"repos/{repo}/issues/{number}")]
    if kind == "pulls":
        sources.append(
            ("pull_request_review_comment", "pull_request", f"repos/{repo}/pulls/{number}")
        )
    payloads: list[tuple[str, dict[str, Any]]] = []
    for event_type, parent_key, parent_path in sources:
        listed, truncated = _get_all(runner, f"{parent_path}/comments?per_page=100&since={iso(since)}")
        if truncated:
            raise FeedError("truncated", parent_path)
        comments = [
            comment
            for comment in listed
            if _created_since(comment, since)
            and not already_recorded(f"{event_type}:{comment.get('id')}")
        ]
        if not comments:
            continue
        parent = gh_get(runner, parent_path).body
        if not isinstance(parent, dict):
            raise FeedError("invalid_body", parent_path)
        payloads.extend(
            (event_type, {
                "action": "created",
                parent_key: parent,
                "comment": comment,
                "repository": repository,
                "sender": comment.get("user") or {},
            })
            for comment in comments
        )
    payloads.sort(key=lambda item: str(item[1]["comment"].get("created_at")))
    return payloads
