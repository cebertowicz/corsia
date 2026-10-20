"""Fetch the existing comments on a GitHub PR.

A finding can live on either of two channels — a review comment thread, or a
PR-level (issue) comment when it has no usable file anchor — so both are
fetched the same way.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from events.connectors.github_reviewer_gate import (
    MERGEABILITY_POLL_ATTEMPTS,
    MERGEABILITY_POLL_INTERVAL_S,
)
from reviewer.gh_api import run_gh

log = logging.getLogger(__name__)
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String = null) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefOid
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          comments(first: 1) { nodes { databaseId } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ReviewThreadState:
    """GitHub's authoritative state for one fingerprinted root comment."""

    thread_id: str
    is_resolved: bool


@dataclass(frozen=True)
class ReviewThreadsSnapshot:
    """One exact PR-head snapshot mapping root comment IDs to threads."""

    head_sha: str
    by_root_comment_id: dict[int, ReviewThreadState]


def fetch_existing_review_comments(
    pr_number: int, repo: str,
) -> list[dict[str, Any]] | None:
    """Fetch all existing PR review comments, or None when history is unreadable."""
    return _fetch_comments(f"repos/{repo}/pulls/{pr_number}/comments", pr_number)


def fetch_existing_issue_comments(
    pr_number: int, repo: str,
) -> list[dict[str, Any]] | None:
    """Fetch all PR-level (issue) comments, or None when history is unreadable."""
    return _fetch_comments(f"repos/{repo}/issues/{pr_number}/comments", pr_number)


def fetch_existing_review_threads(
    pr_number: int, repo: str,
) -> ReviewThreadsSnapshot | None:
    """Fetch all review-thread states and the exact PR head, or None on error."""
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name or "/" in name:
        log.error("reviewer: invalid repo for GraphQL thread lookup: %r", repo)
        return None

    threads: dict[int, ReviewThreadState] = {}
    after: str | None = None
    head_sha: str | None = None
    for _ in range(100):
        args = _review_threads_query_args(owner, name, pr_number, after)
        page = _fetch_review_thread_page(args, head_sha)
        if page is None:
            return None
        head_sha, page_threads, has_next, next_after = page
        if set(page_threads).intersection(threads):
            log.error("reviewer: GraphQL thread lookup repeated a root comment ID")
            return None
        threads.update(page_threads)
        if not has_next:
            return ReviewThreadsSnapshot(head_sha, threads)
        if (
            not isinstance(next_after, str)
            or not next_after
            or next_after == after
        ):
            log.error("reviewer: GraphQL thread lookup returned an invalid cursor")
            return None
        after = next_after

    log.error("reviewer: GraphQL thread lookup exceeded 100 pages")
    return None


def _review_threads_query_args(
    owner: str, name: str, pr_number: int, after: str | None,
) -> list[str]:
    """Build one paginated GitHub GraphQL request."""
    args = [
        "api", "graphql", "-F", f"owner={owner}", "-F", f"name={name}",
        "-F", f"number={pr_number}", "-f", f"query={_REVIEW_THREADS_QUERY}",
    ]
    if after is not None:
        args.extend(["-F", f"after={after}"])
    return args


def _fetch_review_thread_page(
    args: list[str], expected_head: str | None,
) -> tuple[str, dict[int, ReviewThreadState], bool, str | None] | None:
    """Fetch and validate one GraphQL thread page."""
    try:
        result = run_gh(args, timeout=45)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.error("reviewer: GraphQL thread lookup failed: %s", type(exc).__name__)
        return None
    if result.returncode != 0:
        log.error(
            "reviewer: GraphQL thread lookup failed (rc=%d): %s",
            result.returncode, (result.stderr or "")[:240],
        )
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("reviewer: GraphQL thread lookup returned invalid JSON")
        return None
    if not isinstance(payload, dict) or payload.get("errors"):
        log.error("reviewer: GraphQL thread lookup returned errors")
        return None

    pr = _pull_request_page(payload)
    if pr is None:
        return None
    head_sha = pr.get("headRefOid")
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
        log.error("reviewer: GraphQL thread lookup omitted a valid head SHA")
        return None
    if expected_head is not None and head_sha != expected_head:
        log.error("reviewer: PR head changed during thread pagination")
        return None
    return _parse_thread_connection(pr.get("reviewThreads"), head_sha)


def _pull_request_page(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the pull request object from a GraphQL response."""
    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(pull_request, dict):
        log.error("reviewer: GraphQL thread lookup omitted the pull request")
        return None
    return pull_request


def _parse_thread_connection(
    connection: Any, head_sha: str,
) -> tuple[str, dict[int, ReviewThreadState], bool, str | None] | None:
    """Validate thread identity, resolution state, and pagination metadata."""
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        log.error("reviewer: GraphQL thread lookup returned an incomplete connection")
        return None
    threads: dict[int, ReviewThreadState] = {}
    for thread in connection["nodes"]:
        if not _add_thread_state(thread, threads):
            return None
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict) or type(page_info.get("hasNextPage")) is not bool:
        log.error("reviewer: GraphQL thread lookup omitted pagination state")
        return None
    cursor = page_info.get("endCursor")
    return head_sha, threads, page_info["hasNextPage"], cursor


def _add_thread_state(
    thread: Any, threads: dict[int, ReviewThreadState],
) -> bool:
    """Validate and add one thread keyed by its root REST comment ID."""
    if not isinstance(thread, dict):
        log.error("reviewer: GraphQL thread lookup returned a malformed thread")
        return False
    thread_id = thread.get("id")
    is_resolved = thread.get("isResolved")
    comment_connection = thread.get("comments")
    comments = comment_connection.get("nodes") if isinstance(comment_connection, dict) else None
    root = comments[0] if isinstance(comments, list) and comments else None
    root_id = root.get("databaseId") if isinstance(root, dict) else None
    if (
        not isinstance(thread_id, str) or not thread_id
        or type(is_resolved) is not bool
        or type(root_id) is not int or root_id < 1
    ):
        log.error("reviewer: GraphQL thread lookup returned incomplete thread identity/state")
        return False
    if root_id in threads:
        log.error("reviewer: GraphQL thread lookup repeated a root comment ID")
        return False
    threads[root_id] = ReviewThreadState(thread_id, is_resolved)
    return True


def _fetch_comments(
    api_path: str, pr_number: int,
) -> list[dict[str, Any]] | None:
    """Fetch one comment collection, retrying transient unreadable reads.

    One brief GitHub error (rate limit, timeout, 5xx) must not discard a
    finished review, so this mirrors the reviewer gate's bounded PR-info
    retry: re-read on None, return the first readable result, and return
    None after the full budget so callers' error paths are unchanged.
    """
    for attempt in range(MERGEABILITY_POLL_ATTEMPTS):
        comments = _fetch_comments_once(api_path, pr_number)
        if comments is not None:
            return comments
        if attempt + 1 < MERGEABILITY_POLL_ATTEMPTS:
            log.info(
                "reviewer: PR #%d comment history unreadable, re-polling (%d/%d)",
                pr_number, attempt + 1, MERGEABILITY_POLL_ATTEMPTS,
            )
            time.sleep(MERGEABILITY_POLL_INTERVAL_S)
    return None


def _fetch_comments_once(
    api_path: str, pr_number: int,
) -> list[dict[str, Any]] | None:
    """Fetch every page of one comment collection once, or None when unreadable."""
    try:
        result = run_gh(
            ["api", api_path, "--paginate", "--slurp"],
            timeout=30,
        )
        if result.returncode != 0:
            stderr_text = result.stderr or ""
            if "--slurp" in stderr_text or "unknown flag" in stderr_text:
                return _fetch_with_manual_pagination(api_path)
            log.error(
                "gh api fetch comments failed (rc=%d): %s",
                result.returncode, stderr_text[:300],
            )
            return None
        if not result.stdout.strip():
            log.error("gh api fetch comments returned blank stdout")
            return None
        data = json.loads(result.stdout)
        comments = flatten_slurped(data)
        if comments is None:
            log.error("gh api fetch comments returned an incomplete JSON shape")
            return None
        return comments
    except subprocess.TimeoutExpired:
        log.error("gh api fetch comments timed out for PR #%d", pr_number)
        return None
    except json.JSONDecodeError as exc:
        log.error("gh api fetch comments JSON parse error: %s; retrying with manual pagination", exc)
        return _fetch_with_manual_pagination(api_path)
    except FileNotFoundError:
        log.error("gh CLI not found")
        return None


def flatten_slurped(data: Any) -> list[dict[str, Any]] | None:
    """Flatten complete paginated output, or None for a malformed shape."""
    if not isinstance(data, list):
        return None
    out: list[dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict):
            out.append(entry)
        elif isinstance(entry, list) and all(
            isinstance(item, dict) for item in entry
        ):
            out.extend(entry)
        else:
            return None
    return out


def _fetch_with_manual_pagination(
    api_path: str,
) -> list[dict[str, Any]] | None:
    """Fallback fetch when `gh api --slurp` is unavailable."""
    all_comments: list[dict[str, Any]] = []
    page = 1
    while page <= 100:
        try:
            result = run_gh(
                ["api", f"{api_path}?per_page=100&page={page}"],
                timeout=30,
            )
            if result.returncode != 0:
                log.error(
                    "manual pagination page=%d failed (rc=%d): %s",
                    page, result.returncode, (result.stderr or "")[:300],
                )
                return None
            if not result.stdout.strip():
                log.error("manual pagination page=%d returned blank stdout", page)
                return None
            page_data = json.loads(result.stdout)
            if not isinstance(page_data, list) or any(
                not isinstance(comment, dict) for comment in page_data
            ):
                log.error(
                    "manual pagination page=%d returned an unexpected JSON shape",
                    page,
                )
                return None
            if not page_data:
                return all_comments
            all_comments.extend(page_data)
            if len(page_data) < 100:
                return all_comments
            page += 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
            log.error("manual pagination error on page=%d: %s", page, exc)
            return None
    log.error("manual pagination hit 100-page cap; discarding partial results")
    return None
