"""Exact-head review thread state and lifecycle classification."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from events.markers import REVIEW_RESOLVED_MARKER
from infra.product_events import emit_product_event
from reviewer.review_comment_fetch import (
    ReviewThreadState,
    fetch_existing_review_threads,
)
from reviewer.reviewer_finding_identity import _group_by_fingerprint, extract_fingerprint

log = logging.getLogger(__name__)
_RESOLVED_REPLY_MARKER = REVIEW_RESOLVED_MARKER
THREAD_ACTION_KEY = "_review_thread_action"


@dataclass(frozen=True)
class ReviewThreadAction:
    """One reviewed thread transition attached to a classified comment."""

    thread_id: str
    root_comment_id: int
    was_resolved: bool
    reply_needed: bool = True


@dataclass(frozen=True)
class StillRaisedFinding:
    """A current finding carried by an existing visible comment."""

    finding: dict[str, Any]
    carrier_comment: dict[str, Any]


def exact_head_thread_states(
    pr_number: int,
    repo: str,
    head_sha: str,
    review_comments: list[dict[str, Any]],
) -> dict[int, ReviewThreadState] | None:
    """Return thread states only when a complete snapshot matches the reviewed head."""
    has_fingerprinted_root = any(
        not comment.get("in_reply_to_id")
        and not comment.get("issue_url")
        and extract_fingerprint(comment.get("body") or "")
        for comment in review_comments
    )
    if not has_fingerprinted_root:
        return None

    snapshot = fetch_existing_review_threads(pr_number, repo)
    if snapshot is None:
        emit_product_event(
            "reviewer", "thread_lifecycle",
            properties={
                "pr_number": pr_number,
                "head_sha": head_sha,
                "operation": "snapshot",
                "outcome": "failed",
            },
        )
        return None
    if snapshot.head_sha != head_sha:
        log.warning(
            "reviewer: skip thread lifecycle on PR #%d; reviewed %s but current head is %s",
            pr_number, head_sha, snapshot.head_sha,
        )
        emit_product_event(
            "reviewer", "thread_lifecycle",
            properties={
                "pr_number": pr_number,
                "head_sha": head_sha,
                "current_head_sha": snapshot.head_sha,
                "operation": "snapshot",
                "outcome": "stale_head",
            },
        )
        return None
    return snapshot.by_root_comment_id


def classify_with_thread_states(
    new_findings: list[dict[str, Any]],
    existing_comments: list[dict[str, Any]],
    thread_states: dict[int, ReviewThreadState],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[StillRaisedFinding],
]:
    """Plan lifecycle transitions using GitHub's complete thread-state snapshot."""
    new_by_fingerprint = _group_by_fingerprint(new_findings)
    roots_by_fingerprint, pr_resolutions, replies_by_parent = _index_comments(
        existing_comments,
    )
    new_threads: list[dict[str, Any]] = []
    resolve_replies: list[dict[str, Any]] = []
    still_raised: list[StillRaisedFinding] = []

    for fingerprint, roots in roots_by_fingerprint.items():
        _classify_one_fingerprint(
            fingerprint=fingerprint,
            roots=roots,
            new_by_fingerprint=new_by_fingerprint,
            pr_resolutions=pr_resolutions,
            replies_by_parent=replies_by_parent,
            thread_states=thread_states,
            new_threads=new_threads,
            resolve_replies=resolve_replies,
            still_raised=still_raised,
        )

    new_threads.extend(
        finding for fingerprint, finding in new_by_fingerprint.items()
        if fingerprint not in roots_by_fingerprint
    )
    return new_threads, resolve_replies, still_raised


def _index_comments(
    existing_comments: list[dict[str, Any]],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[Any, list[dict[str, Any]]],
]:
    """Group fingerprinted roots, PR-level resolutions, and thread replies."""
    roots_by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    pr_resolutions: dict[str, list[dict[str, Any]]] = {}
    replies_by_parent: dict[Any, list[dict[str, Any]]] = {}
    for comment in existing_comments:
        parent_id = comment.get("in_reply_to_id")
        if parent_id:
            replies_by_parent.setdefault(parent_id, []).append(comment)
            continue
        body = comment.get("body") or ""
        fingerprint = extract_fingerprint(body)
        if not fingerprint:
            continue
        if _RESOLVED_REPLY_MARKER in body:
            if comment.get("issue_url"):
                pr_resolutions.setdefault(fingerprint, []).append(comment)
            continue
        roots_by_fingerprint.setdefault(fingerprint, []).append(comment)
    return roots_by_fingerprint, pr_resolutions, replies_by_parent


def _classify_one_fingerprint(
    *,
    fingerprint: str,
    roots: list[dict[str, Any]],
    new_by_fingerprint: dict[str, dict[str, Any]],
    pr_resolutions: dict[str, list[dict[str, Any]]],
    replies_by_parent: dict[Any, list[dict[str, Any]]],
    thread_states: dict[int, ReviewThreadState],
    new_threads: list[dict[str, Any]],
    resolve_replies: list[dict[str, Any]],
    still_raised: list[StillRaisedFinding],
) -> None:
    """Add one fingerprint's publication, resolution, or carried-finding action."""
    review_roots = [root for root in roots if not root.get("issue_url")]
    pr_roots = [root for root in roots if root.get("issue_url")]
    pr_active = _newest_active_comment(
        pr_roots, set(), pr_resolutions.get(fingerprint, []),
    )
    open_active: list[tuple[dict[str, Any], ReviewThreadState]] = []
    reopenable: list[tuple[dict[str, Any], ReviewThreadState, str]] = []
    human_resolved: list[dict[str, Any]] = []
    for root in review_roots:
        root_id = root.get("id")
        state = thread_states.get(root_id) if type(root_id) is int else None
        if state is None:
            continue
        event = _latest_reviewer_event(
            root, replies_by_parent.get(root_id, []), fingerprint,
        )
        if event == "finding":
            if state.is_resolved:
                human_resolved.append(root)
            else:
                open_active.append((root, state))
        else:
            reopenable.append((root, state, event))

    if fingerprint in new_by_fingerprint:
        _classify_present_finding(
            fingerprint, open_active, reopenable, human_resolved, pr_active,
            new_by_fingerprint, new_threads, still_raised,
        )
        return
    _classify_absent_finding(
        fingerprint, open_active, reopenable, pr_active,
        replies_by_parent, thread_states, resolve_replies,
    )


def _classify_present_finding(
    fingerprint: str,
    open_active: list[tuple[dict[str, Any], ReviewThreadState]],
    reopenable: list[tuple[dict[str, Any], ReviewThreadState, str]],
    human_resolved: list[dict[str, Any]],
    pr_active: dict[str, Any] | None,
    new_by_fingerprint: dict[str, dict[str, Any]],
    new_threads: list[dict[str, Any]],
    still_raised: list[StillRaisedFinding],
) -> None:
    """Keep a current finding unchanged or attach its prior thread for reopening."""
    roots = [root for root, _ in open_active] + human_resolved
    carrier = max(roots, key=_comment_order) if roots else pr_active
    if carrier is not None:
        still_raised.append(StillRaisedFinding(
            new_by_fingerprint[fingerprint], carrier,
        ))
        return
    if reopenable:
        root, state, _ = max(
            reopenable, key=lambda item: _comment_order(item[0]),
        )
        finding = dict(new_by_fingerprint[fingerprint])
        finding[THREAD_ACTION_KEY] = ReviewThreadAction(
            thread_id=state.thread_id,
            root_comment_id=root["id"],
            was_resolved=state.is_resolved,
        )
        new_threads.append(finding)
    else:
        new_threads.append(new_by_fingerprint[fingerprint])



def _classify_absent_finding(
    fingerprint: str,
    open_active: list[tuple[dict[str, Any], ReviewThreadState]],
    reopenable: list[tuple[dict[str, Any], ReviewThreadState, str]],
    pr_active: dict[str, Any] | None,
    replies_by_parent: dict[Any, list[dict[str, Any]]],
    thread_states: dict[int, ReviewThreadState],
    resolve_replies: list[dict[str, Any]],
) -> None:
    """Queue a truthful reply and state transition for an absent fingerprint."""
    open_roots = [root for root, _ in open_active]
    open_roots.extend(
        root for root, state, event in reopenable
        if not state.is_resolved and event == "resolution"
    )
    if not open_roots:
        if pr_active is not None:
            resolve_replies.append(pr_active)
        return

    root = max(open_roots, key=_comment_order)
    state = thread_states[root["id"]]
    event = _latest_reviewer_event(
        root, replies_by_parent.get(root["id"], []), fingerprint,
    )
    resolution = dict(root)
    resolution[THREAD_ACTION_KEY] = ReviewThreadAction(
        thread_id=state.thread_id,
        root_comment_id=root["id"],
        was_resolved=False,
        reply_needed=event != "resolution",
    )
    resolve_replies.append(resolution)


def _latest_reviewer_event(
    root: dict[str, Any],
    replies: list[dict[str, Any]],
    fingerprint: str,
) -> str:
    """Return the latest marked reviewer event in a thread."""
    latest_order = _comment_order(root)
    latest_event = "finding"
    for reply in replies:
        body = reply.get("body") or ""
        if _RESOLVED_REPLY_MARKER in body:
            event = "resolution"
        elif extract_fingerprint(body) == fingerprint:
            event = "finding"
        else:
            continue
        order = _comment_order(reply)
        if order > latest_order:
            latest_order = order
            latest_event = event
    return latest_event


def _newest_active_comment(
    comments: list[dict[str, Any]],
    resolved_thread_ids: set[Any],
    pr_resolutions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the newest unresolved thread or PR-level record for a fingerprint."""
    candidates = [
        comment for comment in comments
        if comment.get("id") not in resolved_thread_ids
    ]
    pr_candidates = [comment for comment in candidates if comment.get("issue_url")]
    if pr_candidates and pr_resolutions:
        cutoff = max(_comment_order(comment) for comment in pr_resolutions)
        candidates = [
            comment for comment in candidates
            if not comment.get("issue_url") or _comment_order(comment) > cutoff
        ]
    return max(candidates, key=_comment_order, default=None)


def _comment_order(comment: dict[str, Any]) -> tuple[str, int]:
    """Order comments by creation time and numeric ID."""
    created = comment.get("created_at")
    identifier = comment.get("id")
    return (
        created if isinstance(created, str) else "",
        identifier if isinstance(identifier, int) and not isinstance(identifier, bool) else -1,
    )
