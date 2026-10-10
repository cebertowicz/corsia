"""Publish a completed review onto its PR and record where each finding landed.

The reviewer's job is to report. A finding it produced but could not publish is
a *lost* finding, not a formatting detail. Issue #946 lost a ``block`` exactly
that way: a finding carrying no ``file`` anchor was counted as failed without
any post being attempted, every shortfall surfaced as one aggregate number, and
findings are persisted nowhere else — so the content was unrecoverable and a
lost block was indistinguishable from a lost nit.

So publication here degrades a finding's channel rather than dropping it
(line -> file -> PR-level comment), keeps a per-finding delivery record,
reconciles what the summary *declares* against what actually landed, and names
every shortfall by severity, category, and location. ``reviewer_threads`` still
owns comment identity, body formatting, classification, and resolution.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from infra.product_events import emit_product_event
from reviewer.gh_api import gh_api_post_json
from reviewer.review_publication_types import (
    CHANNEL_FILE,
    CHANNEL_LINE,
    CHANNEL_NONE,
    CHANNEL_PR_LEVEL,
    CHANNEL_THREAD,
    REASON_ANCHOR_REJECTED,
    REASON_NO_ANCHOR,
    FindingDelivery,
    ReviewPublication,
    ReviewPublicationError,
)
from reviewer.reviewer_categories import is_approach_reset_finding
from reviewer.reviewer_comment import format_summary_body
from reviewer.review_thread_publication import (
    publish_absence_reply,
    reopen_and_republish,
)
from reviewer.reviewer_threads import format_threaded_comment_body, post_review_summary

log = logging.getLogger(__name__)

_PUBLICATION_ANCHOR = "_publication_anchor"


def prepare_findings_for_publication(
    findings: list[dict[str, Any]], *, fallback_file: str = "",
) -> list[dict[str, Any]]:
    """Collapse reset-mode output into one grouped, stop-first finding.
    The approach finding already owns the shared defect and consequence. The
    other findings are evidence for that diagnosis, not an author patch queue,
    so keep their content in the grouped finding and use the first real anchor
    only to make the STOP visible to ``pulls/{pr}/comments`` readers.
    """
    approach = next((
        finding for finding in findings
        if is_approach_reset_finding(finding)
    ), None)
    if approach is None:
        return list(findings)

    related = [finding for finding in findings if finding is not approach]
    grouped = dict(approach)
    if related:
        evidence = []
        for finding in related:
            path = str(finding.get("file") or "(PR-level)")
            line = finding.get("line")
            location = f"{path}:{line}" if line else path
            category = finding.get("category") or "uncategorized"
            description = finding.get("description") or "(no description)"
            evidence.append(
                f"- `{location}` [{category}]: {description}"
            )
        grouped["description"] = (
            f"{grouped.get('description') or '(no description)'}\n\n"
            "Correlated evidence — do not patch individually:\n"
            + "\n".join(evidence)
        )

    anchor = next(
        (finding for finding in related if finding.get("file")), None,
    )
    anchor_file = anchor.get("file") if anchor is not None else fallback_file
    if anchor_file:
        grouped[_PUBLICATION_ANCHOR] = {
            "file": anchor_file,
            "line": anchor.get("line") if anchor is not None else 0,
        }
    return [grouped]


def publish_review(
    *,
    pr_number: int,
    repo: str,
    head_sha: str,
    findings: list[dict[str, Any]],
    new_threads: list[dict[str, Any]],
    resolve_replies: list[dict[str, Any]],
    unchanged_count: int,
    model_label: str,
    existing_issue_comments: list[dict[str, Any]],
) -> ReviewPublication:
    """Publish findings, summary, and resolutions, then reconcile the result.

    The summary is composed *after* the findings are published so it can state
    which of them had to fall back to a PR-level comment.

    Args:
        pr_number: PR the review belongs to.
        repo: Full ``owner/repo`` name.
        head_sha: Reviewed head commit; anchors comments and resolutions.
        findings: Every finding in this run (drives the severity totals).
        new_threads: Findings with no existing comment — the ones to publish.
        resolve_replies: Prior finding comments to mark resolved.
        unchanged_count: Prior findings that still apply.
        model_label: Human model name for the summary.
        existing_issue_comments: PR-level comments already on the PR, used to
            find the sticky summary comment.

    Returns:
        The delivery record for ``new_threads``.

    Raises:
        ReviewPublicationError: when anything the summary declares did not
            actually land on the PR.
    """
    publication = publish_findings(pr_number, repo, head_sha, new_threads)

    summary_body = format_summary_body(
        findings=findings,
        model_label=model_label,
        new_count=len(new_threads),
        unchanged_count=unchanged_count,
        resolved_count=len(resolve_replies),
        pr_level_count=len(publication.pr_level),
    )
    summary_posted = post_review_summary(
        pr_number, repo, summary_body, existing_issue_comments,
    )

    resolutions_posted = sum(
        publish_absence_reply(comment, repo, head_sha, pr_number)
        for comment in resolve_replies
    )

    errors = reconcile_publication(
        publication=publication,
        declared_new=len(new_threads),
        declared_resolved=len(resolve_replies),
        resolutions_posted=resolutions_posted,
        summary_posted=summary_posted,
    )
    if errors:
        raise ReviewPublicationError(
            _failure_message(errors, publication),
            lost_block=bool(publication.lost_blocks),
        )
    return publication


def publish_findings(
    pr_number: int,
    repo: str,
    commit_sha: str,
    findings: list[dict[str, Any]],
) -> ReviewPublication:
    """Deliver every finding, degrading its channel rather than dropping it."""
    return ReviewPublication(
        [_deliver_finding(pr_number, repo, commit_sha, f) for f in findings]
    )


def _deliver_finding(
    pr_number: int,
    repo: str,
    commit_sha: str,
    finding: dict[str, Any],
) -> FindingDelivery:
    """Publish one finding on the most specific channel that accepts it."""
    if reopen_and_republish(pr_number, repo, commit_sha, finding):
        return FindingDelivery(finding, CHANNEL_THREAD)

    body = format_threaded_comment_body(finding)
    anchor = finding.get(_PUBLICATION_ANCHOR)
    if not isinstance(anchor, dict):
        anchor = {}
    path = str(finding.get("file") or anchor.get("file") or "").strip()

    reason = REASON_NO_ANCHOR
    if path:
        channel = _post_anchored_comment(
            pr_number, repo, commit_sha, finding, body, path,
            anchor_line=anchor.get("line"),
        )
        if channel:
            return FindingDelivery(finding, channel)
        reason = REASON_ANCHOR_REJECTED

    if gh_api_post_json(
        f"repos/{repo}/issues/{pr_number}/comments", {"body": body},
    ):
        log.warning(
            "reviewer: PR #%s finding published as a PR-level comment (%s)",
            pr_number, reason,
        )
        delivery = FindingDelivery(finding, CHANNEL_PR_LEVEL, reason)
        _emit_delivery_event("finding.fallback", pr_number, delivery)
        return delivery

    # Last resort: findings live nowhere but GitHub, so a finding that reached
    # no channel is gone unless its full content is recorded here.
    log.error(
        "reviewer: PR #%s finding NOT published (%s, and the PR-level fallback "
        "also failed). Full content preserved here because findings are not "
        "persisted anywhere else: %s",
        pr_number, reason, json.dumps(finding, default=str, sort_keys=True),
    )
    delivery = FindingDelivery(finding, CHANNEL_NONE, reason)
    _emit_delivery_event("finding.lost", pr_number, delivery)
    return delivery


def _emit_delivery_event(
    action: str, pr_number: int, delivery: FindingDelivery,
) -> None:
    """Instrument a degraded or failed delivery so the rate is measurable."""
    emit_product_event(
        "reviewer", action,
        properties={
            "pr_number": pr_number,
            "severity": delivery.finding.get("severity"),
            "category": delivery.finding.get("category"),
            "file": delivery.finding.get("file"),
            "reason": delivery.reason,
        },
    )



def _post_anchored_comment(
    pr_number: int,
    repo: str,
    commit_sha: str,
    finding: dict[str, Any],
    body: str,
    path: str,
    *,
    anchor_line: Any = None,
) -> str:
    """Post a line- or file-level review comment; return its channel or ``""``."""
    try:
        line_int = int(finding.get("line") or anchor_line or 0)
    except (TypeError, ValueError):
        line_int = 0

    payload: dict[str, Any] = {
        "body": body,
        "commit_id": commit_sha,
        "path": path,
    }
    if line_int > 0:
        payload["line"] = line_int
        payload["side"] = "RIGHT"
        channel = CHANNEL_LINE
    else:
        payload["subject_type"] = "file"
        channel = CHANNEL_FILE

    posted = gh_api_post_json(
        f"repos/{repo}/pulls/{pr_number}/comments", payload,
    )
    return channel if posted else ""


def reconcile_publication(
    *,
    publication: ReviewPublication,
    declared_new: int,
    declared_resolved: int,
    resolutions_posted: int,
    summary_posted: bool,
) -> list[str]:
    """Return one specific error per item the summary declares but never landed.

    The summary comment declares ``N new, U still-applies, R resolved``. Nobody
    should have to count PR comments by hand to find out whether those numbers
    are true, so the reviewer checks its own declaration here. Lost blocks come
    first: the reviewer status is truncated, and a lost block is the one
    shortfall that must survive truncation.
    """
    errors = [_lost_message(d) for d in publication.lost_blocks]
    errors += [
        _lost_message(d) for d in publication.undelivered if not d.is_block
    ]

    if len(publication.deliveries) != declared_new:
        errors.append(
            f"delivery record covers {len(publication.deliveries)} of "
            f"{declared_new} declared new finding(s)"
        )
    if not summary_posted:
        errors.append("review summary comment was not published")

    missing_resolutions = declared_resolved - resolutions_posted
    if missing_resolutions:
        errors.append(
            f"{missing_resolutions} of {declared_resolved} declared resolution "
            "reply/replies were not published"
        )
    return errors


def _lost_message(delivery: FindingDelivery) -> str:
    """Name one lost finding precisely enough for a human to act on it."""
    return (
        f"{delivery.label()} NOT PUBLISHED "
        f"({delivery.reason}; PR-level fallback also failed)"
    )


def _failure_message(
    errors: list[str], publication: ReviewPublication,
) -> str:
    """Build the surfaced failure text, leading with any lost block."""
    joined = "; ".join(errors)
    lost_blocks = len(publication.lost_blocks)
    if lost_blocks:
        return f"review LOST {lost_blocks} BLOCK finding(s): {joined}"
    return f"review publication failed: {joined}"
