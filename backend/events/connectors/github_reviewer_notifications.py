"""GitHub reviewer owner notifications delivered on the backend event loop."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from loguru import logger as log

from infra.product_events import emit_product_event

OwnerDeliveryResult = Literal["delivered", "no_owner", "failed"]


def deliver_owner_event(
    repo: str,
    pr_number: int,
    event_type: str,
    summary: str,
    details: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None = None,
    chip: str | None = None,
) -> OwnerDeliveryResult:
    """Deliver one reviewer event on the backend loop from a gate worker.

    ``summary`` is what the owner agent reads; ``chip`` is the shorter line the
    transcript shows when the summary is instructions to the agent.
    """
    try:
        from events.delivery import (
            deliver_and_resolve_to_thread,
            owning_thread_for_identity,
        )
        from events.identity import (
            GITHUB_INTEGRATION,
            PR_TYPE,
            RoutingIdentity,
            pr_external_id,
        )

        external_id = pr_external_id(repo, pr_number)
        owner = owning_thread_for_identity(GITHUB_INTEGRATION, PR_TYPE, external_id)
        if not owner:
            return "no_owner"
        if loop is None:
            log.error("reviewer-gate: owner delivery requires the backend event loop")
            return "failed"
        identity = RoutingIdentity(GITHUB_INTEGRATION, PR_TYPE, external_id)
        delivered = asyncio.run_coroutine_threadsafe(
            deliver_and_resolve_to_thread(
                owner, identity, event_type, summary, details, chip=chip,
            ),
            loop,
        ).result()
        if not delivered:
            log.error(
                "reviewer-gate: owner thread {} not woken for {} on PR #{}",
                owner, event_type, pr_number,
            )
            return "failed"
        return "delivered"
    except Exception as notify_exc:  # noqa: BLE001 — never mask the real error
        log.error(
            "reviewer-gate: failed to notify owner of {} for PR #{}: {}",
            event_type, pr_number, notify_exc,
        )
        return "failed"


def notify_reviewer_error(
    repo: str,
    pr_number: int,
    head_sha: str,
    exc: Exception,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Route a loud ``reviewer.errored`` event to the PR's owning thread."""
    deliver_owner_event(
        repo,
        pr_number,
        "reviewer.errored",
        f"LLM reviewer errored on PR #{pr_number} (retryable)",
        {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha[:8],
            "error": str(exc)[:200],
        },
        loop,
    )


def notify_checks_unreadable(
    repo: str,
    pr_number: int,
    head_sha: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Route a loud ``reviewer.checks_unreadable`` event to the PR owner."""
    deliver_owner_event(
        repo,
        pr_number,
        "reviewer.checks_unreadable",
        (
            f"PR #{pr_number}: check-run fetch unreadable — "
            "retry the existing CI run for this head; "
            "do not push code or an empty/no-op commit to retrigger"
        ),
        {"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        loop,
        chip=f"PR #{pr_number}: check-run fetch unreadable",
    )


def notify_pr_info_unreadable(
    repo: str,
    pr_number: int,
    head_sha: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Route a loud ``reviewer.pr_info_unreadable`` event to the PR owner."""
    deliver_owner_event(
        repo,
        pr_number,
        "reviewer.pr_info_unreadable",
        (
            f"PR #{pr_number}: PR metadata fetch unreadable — "
            "retry the existing CI run for this head; "
            "do not push code or an empty/no-op commit to retrigger"
        ),
        {"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        loop,
        chip=f"PR #{pr_number}: PR metadata fetch unreadable",
    )


def notify_reviewer_verdict(
    repo: str,
    pr_number: int,
    head_sha: str,
    verdict: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None = None,
) -> OwnerDeliveryResult:
    """Wake the registered PR owner when the reviewer reaches a verdict."""
    verdict_name = str(verdict.get("verdict") or "unknown")
    result = deliver_owner_event(
        repo,
        pr_number,
        "reviewer.verdict",
        f"PR #{pr_number} reviewer verdict: {verdict_name}",
        {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "verdict": verdict_name,
            "required_action": verdict.get("required_action", "none"),
            "summary": verdict.get("summary"),
            "block_count": verdict.get("block_count", 0),
            "flag_count": verdict.get("flag_count", 0),
            "nit_count": verdict.get("nit_count", 0),
            "finding_count": verdict.get("finding_count", 0),
            "model": verdict.get("model_label") or verdict.get("model"),
        },
        loop,
    )
    if result == "no_owner":
        log.error(
            "reviewer-gate: no registered owner for reviewer verdict "
            "{} on PR #{} sha={}", verdict_name, pr_number, head_sha[:8],
        )
        emit_product_event(
            "reviewer_gate",
            "reviewer_verdict_owner_missing",
            properties={
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha[:8],
                "verdict": verdict_name,
            },
        )
    return result


def notify_required_check_timeout(
    repo: str,
    pr_number: int,
    head_sha: str,
    workflow_run: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None = None,
) -> OwnerDeliveryResult:
    """Route a required CI timeout through the established PR-owner event path."""
    run_url = str(workflow_run.get("html_url") or "")
    job_name = str(
        workflow_run.get("job_name") or workflow_run.get("name") or workflow_run.get("display_title") or "required check"
    )
    return deliver_owner_event(
        repo,
        pr_number,
        "reviewer.required_check_timeout",
        f"PR #{pr_number}: required check timed out ({job_name}); inspect {run_url}",
        {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "run_url": run_url,
            "job_name": job_name,
        },
        loop,
        chip=f"PR #{pr_number}: required check timed out ({job_name})",
    )


def notify_required_check_cancelled(
    repo: str,
    pr_number: int,
    head_sha: str,
    workflow_run: dict[str, Any],
    reason: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> OwnerDeliveryResult:
    """Route a cancelled required check that reconciliation cannot safely retry."""
    run_url = str(workflow_run.get("html_url") or "")
    job_name = str(
        workflow_run.get("job_name") or workflow_run.get("name") or workflow_run.get("display_title") or "required check"
    )
    return deliver_owner_event(
        repo,
        pr_number,
        "reviewer.required_check_cancelled",
        f"PR #{pr_number}: cancelled required check ({job_name}; {reason}); inspect {run_url}",
        {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "run_url": run_url,
            "job_name": job_name,
            "reason": reason,
        },
        loop,
        chip=f"PR #{pr_number}: cancelled required check ({reason})",
    )
