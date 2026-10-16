"""Reviewer gate routing."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from events import guard
from infra.product_events import emit_product_event

REVIEWER_EVENTS = frozenset(
    {"webhook.github.pr_opened", "webhook.github.pr_synchronize"}
)

GATE_TRIGGER_EVENTS = frozenset({
    "webhook.github.check_suite_completed",
    "webhook.github.pr_ready_for_review",
    "webhook.github.pr_labeled",
})


def run_review(internal_event: str, payload: dict[str, Any]) -> None:
    """Handle reviewer push events without arming a review; hold a fork head for approval."""
    if internal_event not in REVIEWER_EVENTS:
        return

    if guard.is_bot_or_self_authored(payload):
        author = payload.get("sender") or {}
        logger.info(
            "reviewer-route: bot/self-authored push {} suppressed ({})",
            internal_event,
            author.get("login", "?"),
        )
        emit_product_event(
            "reviewer",
            "suppressed",
            properties={
                "internal_event": internal_event,
                "author": author.get("login"),
                "reason": "bot_or_self_author",
            },
        )
        return

    pr = payload.get("pull_request") or {}
    pr_number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha", "")
    logger.debug(
        "reviewer-route: push event {} PR #{} sha={} — gate evaluates on green CI or ready transition",
        internal_event,
        pr_number,
        (head_sha or "?")[:8],
    )

    from events.connectors.github_reviewer_fork_hold import on_fork_push

    repo = (payload.get("repository") or {}).get("full_name", "")
    on_fork_push(repo, pr_number, head_sha, internal_event, payload)


def check_gate_on_trigger(
    internal_event: str,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None = None,
) -> bool:
    """Evaluate gate triggers; reject malformed ready-event payloads visibly."""
    if internal_event not in GATE_TRIGGER_EVENTS:
        return False

    if internal_event == "webhook.github.pr_ready_for_review":
        return _check_ready_event(internal_event, payload, loop)

    if internal_event == "webhook.github.pr_labeled":
        return _check_label_event(internal_event, payload, loop)

    repo = (payload.get("repository") or {}).get("full_name", "")
    suite = payload.get("check_suite") or {}
    head_sha = suite.get("head_sha", "")
    conclusion = suite.get("conclusion", "") or ""

    # Shared resolver: array-first, head_sha fallback when GitHub delivers an
    # empty pull_requests array (else the gate silently skips a green PR).
    from events.identity import PrResolutionError, pr_number_from_check_suite_event

    try:
        pr_number = pr_number_from_check_suite_event(payload)
    except PrResolutionError as exc:
        logger.warning("reviewer-route: PR resolution failed for {}: {}", head_sha[:8], exc)
        if repo and head_sha:
            from reviewer.reviewer_status import set_error

            set_error(repo, head_sha, f"qa/llm-reviewer: PR resolution error — {str(exc)[:60]}")
        emit_product_event(
            "reviewer",
            "pr_resolution_failed",
            properties={
                "internal_event": internal_event,
                "repo": repo,
                "head_sha": head_sha[:8],
                "error": str(exc)[:200],
            },
        )
        return True

    if not head_sha or not repo or not pr_number:
        logger.debug(
            "reviewer-route: check-suite event missing head_sha/repo/pr_number, skipping"
        )
        return True

    from events.reviewer_gate import on_check_suite_completed

    on_check_suite_completed(
        repo=repo,
        head_sha=head_sha,
        conclusion=conclusion,
        pr_number=pr_number,
        internal_event=internal_event,
        loop=loop,
    )
    return True


def _missing_identity(repo: Any, pr_number: Any, head_sha: Any) -> list[str]:
    """Name the identity fields a pull request event lacks."""
    missing_fields = []
    if not isinstance(repo, str) or not repo:
        missing_fields.append("repository.full_name")
    if (
        not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number <= 0
    ):
        missing_fields.append("pull_request.number")
    if not isinstance(head_sha, str) or not head_sha:
        missing_fields.append("pull_request.head.sha")
    return missing_fields


def _check_ready_event(
    internal_event: str,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
) -> bool:
    """Validate ready-event identity before invoking the shared gate."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    pr = payload.get("pull_request") or {}
    head_sha = (pr.get("head") or {}).get("sha", "")
    pr_number = pr.get("number")
    missing_fields = _missing_identity(repo, pr_number, head_sha)
    action = payload.get("action")
    if missing_fields:
        _report_invalid_ready_event(
            repo, pr_number, head_sha, action, missing_fields,
        )
        return False

    from events.reviewer_gate import on_pull_request_ready_for_review

    on_pull_request_ready_for_review(
        repo=repo,
        head_sha=head_sha,
        pr_number=pr_number,
        internal_event=internal_event,
        loop=loop,
    )
    return True


def _check_label_event(
    internal_event: str,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
) -> bool:
    """Treat the review label as approval of the head named in the payload.

    Only a maintainer can add a label, so the event itself is the authority;
    the sender is recorded with the approval.
    """
    from reviewer.reviewer_categories import LABEL_REVIEW

    repo = (payload.get("repository") or {}).get("full_name", "")
    pr = payload.get("pull_request") or {}
    head_sha = (pr.get("head") or {}).get("sha", "")
    pr_number = pr.get("number")
    missing_fields = _missing_identity(repo, pr_number, head_sha)
    if missing_fields:
        _report_invalid_ready_event(
            repo, pr_number, head_sha, payload.get("action"), missing_fields,
        )
        return False
    if (payload.get("label") or {}).get("name") != LABEL_REVIEW:
        return True

    from events.connectors.github_reviewer_fork_hold import approve_head

    approve_head(
        repo, pr_number, head_sha, (payload.get("sender") or {}).get("login"),
        internal_event, loop,
    )
    return True


def _report_invalid_ready_event(
    repo: str,
    pr_number: Any,
    head_sha: Any,
    action: Any,
    missing_fields: list[str],
) -> None:
    """Emit a clear diagnostic for a ready event with incomplete identity."""
    reason = "missing_identity"
    logger.warning(
        "reviewer-route: ready event unusable reason={} missing_fields={}",
        reason, missing_fields,
    )
    emit_product_event(
        "reviewer_gate",
        "ready_event_invalid",
        properties={
            "repo": repo or None,
            "pr_number": pr_number,
            "head_sha": head_sha[:8] if isinstance(head_sha, str) else None,
            "action": action,
            "reason": reason,
            "missing_fields": missing_fields,
        },
    )
