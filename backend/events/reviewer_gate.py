"""Event-driven final gate for the LLM reviewer."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from loguru import logger as log

from events.connectors import github_reviewer_gate, github_reviewer_notifications
from events import reviewer_gate_state
from infra.product_events import emit_product_event


MERGEABILITY_RETRY_DELAYS_S = (30.0, 90.0, 300.0)
_PENDING_RETRIES: set[asyncio.Task[None]] = set()


def _run_reviewer(
    repo: str,
    pr_number: int,
    head_sha: str,
    base_ref: str,
    internal_event: str,
    pr_payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Invoke the LLM reviewer and mark this SHA reviewed on success."""
    from reviewer.on_pr_review import on_pr_review

    try:
        verdict = on_pr_review(internal_event, pr_payload)
    except Exception as exc:
        log.error(
            "reviewer-gate: reviewer raised for PR #{}: {} — "
            "skip-guard NOT written; gate remains retryable",
            pr_number, exc,
        )
        emit_product_event(
            "reviewer_gate", "reviewer_error",
            properties={
                "repo": repo,
                "pr_number": pr_number,
                "error": str(exc)[:200],
            },
        )
        github_reviewer_notifications.notify_reviewer_error(
            repo, pr_number, head_sha, exc, loop,
        )
        return

    _settle_reviewer_verdict(
        repo, pr_number, head_sha, base_ref, verdict, loop,
    )


def _settle_reviewer_verdict(
    repo: str,
    pr_number: int,
    head_sha: str,
    base_ref: str,
    verdict: Any,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Record one terminal reviewer result after its blocking evaluation."""
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)

    if verdict is None:
        log.error(
            "reviewer-gate: reviewer produced no terminal result for PR #{} "
            "sha={} — skip-guard NOT written; gate remains retryable",
            pr_number, head_sha[:8],
        )
        emit_product_event(
            "reviewer_gate", "reviewer_no_terminal_result",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        return

    if isinstance(verdict, dict) and verdict.get("verdict") == "skipped":
        reviewer_gate_state.mark_reviewed(pr_key, head_sha)
        log.info(
            "reviewer-gate: PR #{} sha={} base={} — marked reviewed after skip",
            pr_number,
            head_sha[:8],
            base_ref,
        )
        return

    if not isinstance(verdict, dict):
        log.error(
            "reviewer-gate: reviewer returned unexpected result {!r} for PR #{} "
            "sha={} — skip-guard NOT written; gate remains retryable",
            verdict, pr_number, head_sha[:8],
        )
        emit_product_event(
            "reviewer_gate", "reviewer_unexpected_result",
            properties={
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha[:8],
                "result_type": type(verdict).__name__,
            },
        )
        return

    delivery_result = github_reviewer_notifications.notify_reviewer_verdict(
        repo, pr_number, head_sha, verdict, loop,
    )
    if delivery_result == "failed":
        from reviewer.reviewer_status import set_error

        set_error(
            repo,
            head_sha,
            "qa/llm-reviewer: owner wake delivery failed; retry required",
        )
        return

    reviewer_gate_state.mark_reviewed(pr_key, head_sha)
    log.info(
        "reviewer-gate: PR #{} sha={} base={} — marked reviewed",
        pr_number,
        head_sha[:8],
        base_ref,
    )


def on_check_suite_completed(
    repo: str,
    head_sha: str,
    conclusion: str,
    pr_number: int,
    internal_event: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Evaluate the reviewer gate when a GitHub check suite completes."""
    if not pr_number or not repo or not head_sha:
        log.debug("reviewer-gate: check-suite event missing required fields, skipping")
        return

    if conclusion != "success":
        log.info(
            "reviewer-gate: PR #{} sha={} — check_suite conclusion={!r}, not green",
            pr_number, head_sha[:8], conclusion,
        )
        return

    _evaluate_review_gate(repo, head_sha, pr_number, internal_event, loop)


def on_pull_request_ready_for_review(
    repo: str,
    head_sha: str,
    pr_number: int,
    internal_event: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Re-evaluate the gate when a draft PR becomes ready for review."""
    if not pr_number or not repo or not head_sha:
        log.debug("reviewer-gate: ready event missing required fields, skipping")
        return

    _evaluate_review_gate(repo, head_sha, pr_number, internal_event, loop)


def _evaluate_review_gate(
    repo: str,
    head_sha: str,
    pr_number: int,
    internal_event: str,
    loop: asyncio.AbstractEventLoop | None,
    mergeability_retry_attempt: int = 0,
) -> None:
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    if reviewer_gate_state.get_reviewed_sha(pr_key) == head_sha:
        log.info(
            "reviewer-gate: PR #{} sha={} — skip-guard (already reviewed), no-op",
            pr_number, head_sha[:8],
        )
        emit_product_event(
            "reviewer_gate", "skip_guard",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        return

    checks_result = github_reviewer_gate.checks_green(repo, pr_number, head_sha)
    if checks_result is None:
        log.error(
            "reviewer-gate: PR #{} sha={} — check fetch unreadable; "
            "emitting checks_unreadable, notifying owner",
            pr_number, head_sha[:8],
        )
        emit_product_event(
            "reviewer_gate", "checks_unreadable",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        github_reviewer_notifications.notify_checks_unreadable(
            repo, pr_number, head_sha, loop,
        )
        return
    if not checks_result:
        emit_product_event(
            "reviewer_gate", "checks_not_green",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        return

    _evaluate_ready_pr(
        repo,
        pr_number,
        head_sha,
        internal_event,
        loop,
        mergeability_retry_attempt,
    )


def _evaluate_ready_pr(
    repo: str,
    pr_number: int,
    head_sha: str,
    internal_event: str,
    loop: asyncio.AbstractEventLoop | None = None,
    mergeability_retry_attempt: int = 0,
) -> None:
    """Run the reviewer for a checks-green, non-draft PR."""
    pr_info = github_reviewer_gate.fetch_pr_info_awaiting_mergeability(repo, pr_number, head_sha)
    if pr_info is None:
        _report_pr_info_unreadable(repo, pr_number, head_sha, loop)
        return
    if pr_info.get("headRefOid") != head_sha:
        log.info(
            "reviewer-gate: PR #{} sha={} — no longer current head, holding review",
            pr_number, head_sha[:8],
        )
        return
    if not pr_info["isDraft"] and github_reviewer_gate.mergeability_is_unknown(pr_info):
        if _schedule_mergeability_retry(
            loop,
            repo,
            head_sha,
            pr_number,
            internal_event,
            mergeability_retry_attempt,
        ):
            return
        _report_mergeability_unresolved(repo, pr_number, head_sha)
        return
    if github_reviewer_gate.hold_pr_until_reviewable(repo, pr_number, head_sha, pr_info):
        return

    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    if not reviewer_gate_state.claim_in_flight(pr_key, head_sha):
        log.info(
            "reviewer-gate: PR #{} sha={} — eligible review already in flight",
            pr_number, head_sha[:8],
        )
        return
    try:
        if reviewer_gate_state.get_reviewed_sha(pr_key) == head_sha:
            return
        base_ref = pr_info.get("baseRefName", "")
        _report_review_firing(repo, pr_number, head_sha)
        pr_payload: dict[str, Any] = {
            "action": "synchronize",
            "pull_request": {
                "number": pr_number,
                "head": {"sha": head_sha},
                "base": {"ref": base_ref, "sha": pr_info.get("baseRefOid", "")},
                "labels": pr_info.get("labels", []),
                "title": pr_info.get("title", ""),
                "body": pr_info.get("body", ""),
            },
            "repository": {"full_name": repo},
        }
        _run_reviewer(
            repo, pr_number, head_sha, base_ref, internal_event, pr_payload, loop,
        )
    finally:
        reviewer_gate_state.release_in_flight(pr_key, head_sha)


def _schedule_mergeability_retry(
    loop: asyncio.AbstractEventLoop | None,
    repo: str,
    head_sha: str,
    pr_number: int,
    internal_event: str,
    retry_attempt: int,
) -> bool:
    """Schedule bounded, process-local re-evaluation of an unresolved PR head."""
    if loop is None or loop.is_closed() or retry_attempt >= len(MERGEABILITY_RETRY_DELAYS_S):
        return False
    delay = MERGEABILITY_RETRY_DELAYS_S[retry_attempt]
    loop.call_soon_threadsafe(
        loop.call_later,
        delay,
        _dispatch_mergeability_retry,
        repo,
        head_sha,
        pr_number,
        internal_event,
        loop,
        retry_attempt + 1,
    )
    log.info(
        "reviewer-gate: PR #{} sha={} — mergeability unresolved; retrying in {}s",
        pr_number, head_sha[:8], delay,
    )
    return True


def _dispatch_mergeability_retry(
    repo: str,
    head_sha: str,
    pr_number: int,
    internal_event: str,
    loop: asyncio.AbstractEventLoop,
    retry_attempt: int,
) -> None:
    """Return a delayed retry to the normal worker-thread eligibility path."""
    task = asyncio.create_task(asyncio.to_thread(
        _evaluate_review_gate,
        repo, head_sha, pr_number, internal_event, loop, retry_attempt,
    ))
    _PENDING_RETRIES.add(task)
    task.add_done_callback(_PENDING_RETRIES.discard)
    task.add_done_callback(partial(_log_mergeability_retry_failure, pr_number, head_sha))


def _log_mergeability_retry_failure(
    pr_number: int,
    head_sha: str,
    task: asyncio.Task[None],
) -> None:
    """Retrieve and identify any delayed retry failure after task completion."""
    try:
        task.result()
    except asyncio.CancelledError:
        log.info("reviewer-gate: delayed retry cancelled for PR #{} sha={}", pr_number, head_sha[:8])
    except Exception:
        log.exception("reviewer-gate: delayed retry raised for PR #{} sha={}", pr_number, head_sha[:8])


def _report_mergeability_unresolved(repo: str, pr_number: int, head_sha: str) -> None:
    """Make exhausted process-local mergeability retries visible to operators."""
    log.warning(
        "reviewer-gate: PR #{} sha={} — mergeability remained UNKNOWN after retries",
        pr_number,
        head_sha[:8],
    )
    emit_product_event(
        "reviewer_gate",
        "mergeability_unresolved",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
    )


def _report_pr_info_unreadable(
    repo: str,
    pr_number: int,
    head_sha: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Record and report a PR metadata read failure."""
    log.error(
        "reviewer-gate: PR #{} sha={} — PR info fetch unreadable; "
        "emitting pr_info_unreadable, notifying owner",
        pr_number, head_sha[:8],
    )
    from reviewer.reviewer_status import set_error

    set_error(repo, head_sha, "qa/llm-reviewer: could not fetch PR metadata")
    emit_product_event(
        "reviewer_gate", "pr_info_unreadable",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
    )
    github_reviewer_notifications.notify_pr_info_unreadable(
        repo, pr_number, head_sha, loop,
    )



def _report_review_firing(repo: str, pr_number: int, head_sha: str) -> None:
    """Record that every gate condition passed and review is starting."""
    log.info(
        "reviewer-gate: PR #{} sha={} — all conditions met, firing reviewer",
        pr_number, head_sha[:8],
    )
    emit_product_event(
        "reviewer_gate", "firing",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
    )
