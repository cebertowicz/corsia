"""Review comment identity, classification, and summary publication."""

from __future__ import annotations

import logging
import re
from typing import Any

from events.markers import REVIEW_RESOLVED_MARKER, REVIEW_SUMMARY_MARKER
from reviewer.gh_api import gh_api_patch_json, gh_api_post_json
from reviewer.review_comment_fetch import ReviewThreadState
from reviewer.review_thread_lifecycle import (
    THREAD_ACTION_KEY,
    ReviewThreadAction,
    _comment_order,
    _newest_active_comment,
    classify_with_thread_states,
    exact_head_thread_states,
)
from reviewer.reviewer_categories import APPROACH_CHURN_CATEGORY, VALID_CATEGORIES_SET
from reviewer.reviewer_finding_identity import (  # noqa: F401
    _MARKER_FMT,
    _MARKER_RE,
    _group_by_fingerprint,
    extract_fingerprint,
    fingerprint_finding,
)
from reviewer.reviewer_resolution import (  # noqa: F401
    build_pr_level_resolution_body,
    build_resolve_reply_body,
    post_resolve_reply,
)

log = logging.getLogger(__name__)

_RESOLVED_REPLY_MARKER = REVIEW_RESOLVED_MARKER
_HISTORY_BADGE_RE = re.compile(
    r"(?:\*\*(BLOCK|FLAG)\*\*|(NIT))\s+`\[([^]]+)]`"
)
_HISTORY_LIMIT = 40
_HISTORY_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+\-]{0,299}\Z")

def format_threaded_comment_body(finding: dict[str, Any]) -> str:
    """Format a single finding as a threaded review-comment body."""
    severity = finding.get("severity", "nit").upper()
    category = finding.get("category", "uncategorized")
    description = finding.get("description", "(no description)")
    cited = finding.get("cited_section")
    badge = {
        "BLOCK": ":no_entry_sign: **BLOCK**",
        "FLAG": ":warning: **FLAG**",
        "NIT": ":pencil2: NIT",
    }.get(severity, severity)

    lines: list[str] = []
    if category == APPROACH_CHURN_CATEGORY:
        lines.extend([
            ":no_entry_sign: **STOP — APPROACH RESET REQUIRED**",
            "",
            "**Do not patch the evidence below one-by-one. Regroup around the "
            "shared cause before revising.**",
            "",
        ])
    lines.extend([
        f"{badge} `[{category}]`",
        "",
        description,
    ])
    if cited:
        lines.extend(["", f"Ref: _{cited}_"])
    lines.extend(["", _MARKER_FMT.format(fp=fingerprint_finding(finding))])
    return "\n".join(lines)


def build_review_history(comments: list[dict[str, Any]]) -> str:
    """Render bounded structured prior findings without comment prose."""
    rows: list[str] = []
    for comment in comments:
        body = comment.get("body") or ""
        if (
            not extract_fingerprint(body)
            or comment.get("in_reply_to_id")
            or _RESOLVED_REPLY_MARKER in body
        ):
            continue
        badge = _HISTORY_BADGE_RE.search(body)
        if not badge:
            continue
        severity = badge.group(1) or badge.group(2)
        category = badge.group(3)
        path = comment.get("path") or "(PR-level)"
        if category not in VALID_CATEGORIES_SET:
            continue
        if path != "(PR-level)" and (
            not isinstance(path, str) or not _HISTORY_PATH_RE.fullmatch(path)
        ):
            continue
        rows.append(f"{severity} [{category}] {path}")
    if not rows:
        return "No prior reviewer findings are available."
    return (
        "Prior structured findings follow. They are trajectory evidence; "
        "round count alone is not a defect.\n- " + "\n- ".join(rows[-_HISTORY_LIMIT:])
    )


def classify_findings(
    new_findings: list[dict[str, Any]],
    existing_comments: list[dict[str, Any]],
    *,
    review_thread_states: dict[int, ReviewThreadState] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Classify findings, using GitHub thread state when a complete snapshot exists."""
    if review_thread_states is None:
        return _classify_by_markers(new_findings, existing_comments)
    return classify_with_thread_states(
        new_findings, existing_comments, review_thread_states,
    )


def _classify_by_markers(
    new_findings: list[dict[str, Any]],
    existing_comments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Keep legacy publication behavior when GitHub thread state is unavailable."""
    new_by_fingerprint = _group_by_fingerprint(new_findings)
    finding_comments: dict[str, list[dict[str, Any]]] = {}
    pr_resolutions: dict[str, list[dict[str, Any]]] = {}
    resolved_thread_ids: set[Any] = set()

    for comment in existing_comments:
        body = comment.get("body") or ""
        parent_id = comment.get("in_reply_to_id")
        if parent_id:
            if _RESOLVED_REPLY_MARKER in body:
                resolved_thread_ids.add(parent_id)
            continue
        fingerprint = extract_fingerprint(body)
        if not fingerprint:
            continue
        if _RESOLVED_REPLY_MARKER in body:
            if comment.get("issue_url"):
                pr_resolutions.setdefault(fingerprint, []).append(comment)
            continue
        finding_comments.setdefault(fingerprint, []).append(comment)

    active: dict[str, dict[str, Any]] = {}
    for fingerprint, comments in finding_comments.items():
        active_comment = _newest_active_comment(
            comments, resolved_thread_ids, pr_resolutions.get(fingerprint, []),
        )
        if active_comment is not None:
            active[fingerprint] = active_comment
    new_threads = [
        finding for fingerprint, finding in new_by_fingerprint.items()
        if fingerprint not in active
    ]
    resolve_replies = [
        comment for fingerprint, comment in active.items()
        if fingerprint not in new_by_fingerprint
    ]
    unchanged_count = sum(
        1 for fingerprint in new_by_fingerprint if fingerprint in active
    )
    return new_threads, resolve_replies, unchanged_count


def classify_review_findings(
    raw_findings: list[dict[str, Any]],
    grouped_findings: list[dict[str, Any]],
    existing_comments: list[dict[str, Any]],
    pr_number: int,
    repo: str,
    head_sha: str,
    *,
    approach_reset: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Classify a completed review using lifecycle state from its exact head."""
    thread_states = exact_head_thread_states(
        pr_number, repo, head_sha, existing_comments,
    )
    has_exact_head_snapshot = thread_states is not None
    kwargs = {"review_thread_states": thread_states} if has_exact_head_snapshot else {}
    new_threads, resolve_replies, unchanged_count = classify_findings(
        raw_findings, existing_comments, **kwargs,
    )
    if not has_exact_head_snapshot:
        resolve_replies = [
            comment for comment in resolve_replies if comment.get("issue_url")
        ]
    if not approach_reset:
        return new_threads, resolve_replies, unchanged_count
    grouped_new, _, grouped_unchanged = classify_findings(
        grouped_findings, existing_comments, **kwargs,
    )
    return grouped_new, resolve_replies, grouped_unchanged




_SUMMARY_MARKER = REVIEW_SUMMARY_MARKER


def build_summary_body(body: str) -> str:
    """Append the sticky summary marker to a review summary body."""
    return f"{body}\n\n{_SUMMARY_MARKER}"


def post_review_summary(
    pr_number: int,
    repo: str,
    body: str,
    existing_comments: list[dict[str, Any]],
) -> bool:
    """Post or edit the sticky top-level review summary comment."""
    marker_body = build_summary_body(body)
    addressable, existing_id = _find_summary_comment_id(existing_comments)
    if not addressable:
        return False
    if existing_id is not None:
        return gh_api_patch_json(
            f"repos/{repo}/issues/comments/{existing_id}",
            {"body": marker_body},
        )
    return gh_api_post_json(
        f"repos/{repo}/issues/{pr_number}/comments",
        {"body": marker_body},
    )


def _find_summary_comment_id(
    existing_comments: list[dict[str, Any]],
) -> tuple[bool, int | None]:
    """Return whether the sticky summary is addressable, and its comment ID."""
    for comment in existing_comments:
        if _SUMMARY_MARKER not in (comment.get("body") or ""):
            continue
        comment_id = comment.get("id")
        if type(comment_id) is int:
            return True, comment_id
        return False, None
    return True, None
