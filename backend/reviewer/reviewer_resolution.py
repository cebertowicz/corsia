"""Resolution evidence and publishing for prior reviewer findings."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, TypeGuard

from events.markers import REVIEW_RESOLVED_MARKER
from reviewer.gh_api import gh_api_post_json, run_gh
from reviewer.reviewer_finding_identity import _MARKER_FMT, extract_fingerprint

log = logging.getLogger(__name__)

_RESOLVED_REPLY_MARKER = REVIEW_RESOLVED_MARKER
_SHA_RE = re.compile(r"[0-9a-f]{7,40}\Z")
_HUNK_RE = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? ")


def build_resolve_reply_body(report: str) -> str:
    """Build an honest reply for a finding absent from the current review run."""
    return f"Not raised this run. {report}\n\n{_RESOLVED_REPLY_MARKER}"


def build_pr_level_resolution_body(fingerprint: str, report: str) -> str:
    """Build the standalone resolution record for a PR-level finding."""
    return (
        f"{build_resolve_reply_body(report)}\n"
        f"{_MARKER_FMT.format(fp=fingerprint)}"
    )


def set_review_thread_resolved(thread_id: str, *, resolved: bool) -> bool:
    """Set GitHub's review-thread state and verify the mutation response."""
    if not isinstance(thread_id, str) or not thread_id:
        log.error("set_review_thread_resolved: missing GitHub thread ID")
        return False
    mutation = "resolveReviewThread" if resolved else "unresolveReviewThread"
    query = (
        f"mutation($threadId: ID!) {{ {mutation}(input: {{threadId: $threadId}}) "
        "{ thread { id isResolved } } }"
    )
    try:
        result = run_gh(
            ["api", "graphql", "-f", f"query={query}", "-F", f"threadId={thread_id}"],
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.error("reviewer: %s failed: %s", mutation, type(exc).__name__)
        return False
    if result.returncode != 0:
        log.error(
            "reviewer: %s failed (rc=%d): %s",
            mutation, result.returncode, (result.stderr or "")[:240],
        )
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("reviewer: %s returned invalid JSON", mutation)
        return False
    if not isinstance(payload, dict) or payload.get("errors"):
        log.error("reviewer: %s returned GraphQL errors", mutation)
        return False
    data = payload.get("data")
    response = data.get(mutation) if isinstance(data, dict) else None
    thread = response.get("thread") if isinstance(response, dict) else None
    if (
        not isinstance(thread, dict)
        or thread.get("id") != thread_id
        or type(thread.get("isResolved")) is not bool
        or thread["isResolved"] is not resolved
    ):
        log.error("reviewer: %s response did not confirm requested state", mutation)
        return False
    return True


def post_review_thread_reply(
    root_comment_id: int, pr_number: int, repo: str, body: str,
) -> bool:
    """Publish a re-raised finding as a reply in its original review thread."""
    if type(root_comment_id) is not int or root_comment_id < 1:
        log.error("post_review_thread_reply: invalid review comment ID")
        return False
    return gh_api_post_json(
        f"repos/{repo}/pulls/{pr_number}/comments",
        {"body": body, "in_reply_to": root_comment_id},
    )


def post_resolve_reply(comment: dict[str, Any], repo: str, head_sha: str) -> bool:
    """Record an absent finding with citation evidence on its existing channel."""
    comment_id = comment.get("id")
    if not comment_id:
        return False

    report = cited_line_report(comment, repo, head_sha)
    if comment.get("issue_url"):
        return _resolve_pr_level_comment(comment, repo, report)

    pr_number = _pr_number(comment.get("pull_request_url") or "")
    if not pr_number:
        log.error("post_resolve_reply: could not derive PR number from comment")
        return False
    return gh_api_post_json(
        f"repos/{repo}/pulls/{pr_number}/comments",
        {"body": build_resolve_reply_body(report), "in_reply_to": comment_id},
    )


def cited_line_report(
    comment: dict[str, Any], repo: str, head_sha: str,
) -> str:
    """Describe whether the finding's own cited source lines changed at exact SHAs."""
    path = comment.get("path")
    end_line = comment.get("original_line")
    if end_line is None:
        end_line = comment.get("line")
    start_line = comment.get("original_start_line")
    if start_line is None:
        start_line = end_line
    source_sha = comment.get("original_commit_id") or comment.get("commit_id")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or start_line < 1
        or end_line < start_line
    ):
        return "No cited lines; not verified fixed."
    if not _valid_sha(source_sha) or not _valid_sha(head_sha):
        return "Could not check cited lines: finding commit is unavailable; not verified fixed."
    if source_sha == head_sha:
        return _changed_report(False, source_sha, head_sha)

    data, error = _compare_commits(repo, source_sha, head_sha)
    if error or data is None:
        return f"Could not check cited lines: {error}; not verified fixed."
    changed_file = _matching_changed_file(data, path)
    if changed_file is None:
        if len(data["files"]) >= 300:
            return "Could not check cited lines: comparison may omit files; not verified fixed."
        return _changed_report(False, source_sha, head_sha)
    patch = changed_file.get("patch")
    if not isinstance(patch, str):
        return "Could not check cited lines: comparison omitted the file patch; not verified fixed."
    return _changed_report(
        _cited_range_changed(patch, start_line, end_line), source_sha, head_sha,
    )


def _compare_commits(
    repo: str, source_sha: str, head_sha: str,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch the GitHub comparison for the exact finding and reviewed commits."""
    try:
        result = run_gh(
            ["api", f"repos/{repo}/compare/{source_sha}...{head_sha}"],
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if result.returncode:
        return None, (result.stderr or "GitHub compare failed").strip()[:160]
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "GitHub compare returned invalid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None, "GitHub compare returned no file inventory"
    if data.get("status") not in {"ahead", "identical"}:
        return None, "finding commit does not precede reviewed head"
    return data, ""


def _matching_changed_file(
    comparison: dict[str, Any] | None, path: str,
) -> dict[str, Any] | None:
    """Return the changed file record for the original cited path, if present."""
    if comparison is None:
        return None
    for changed_file in comparison["files"]:
        if not isinstance(changed_file, dict):
            continue
        if path in {
            changed_file.get("filename"), changed_file.get("previous_filename"),
        }:
            return changed_file
    return None


def _cited_range_changed(patch: str, start_line: int, end_line: int) -> bool:
    """Return whether a unified-diff hunk changes the cited original range."""
    source_line: int | None = None
    for patch_line in patch.splitlines():
        match = _HUNK_RE.match(patch_line)
        if match:
            source_line = int(match.group("start"))
            continue
        if source_line is None:
            continue
        if patch_line.startswith("-"):
            if start_line <= source_line <= end_line:
                return True
            source_line += 1
        elif patch_line.startswith("+"):
            if start_line < source_line <= end_line:
                return True
        elif patch_line.startswith(" "):
            source_line += 1
    return False


def _changed_report(changed: bool, source_sha: str, head_sha: str) -> str:
    """Render the exact-SHA evidence sentence without implying the finding is fixed."""
    state = "changed" if changed else "unchanged"
    return (
        f"Cited lines {state} since finding commit "
        f"(`{source_sha[:8]}` → `{head_sha[:8]}`); not verified fixed."
    )


def _valid_sha(value: Any) -> TypeGuard[str]:
    """Return whether a value can safely identify an exact Git commit."""
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _resolve_pr_level_comment(
    comment: dict[str, Any], repo: str, report: str,
) -> bool:
    """Post a non-destructive resolution record for a PR-level finding."""
    fingerprint = extract_fingerprint(comment.get("body"))
    if not fingerprint:
        log.error("_resolve_pr_level_comment: comment %s carries no fingerprint", comment.get("id"))
        return False
    pr_number = _pr_number(comment.get("issue_url") or "")
    if not pr_number:
        log.error("_resolve_pr_level_comment: could not derive PR number from comment")
        return False
    return gh_api_post_json(
        f"repos/{repo}/issues/{pr_number}/comments",
        {"body": build_pr_level_resolution_body(fingerprint, report)},
    )


def _pr_number(url: str) -> str | None:
    """Extract a PR number from a GitHub pull or issue API URL."""
    match = re.search(r"/(?:pulls|issues)/(\d+)$", url)
    return match.group(1) if match else None
