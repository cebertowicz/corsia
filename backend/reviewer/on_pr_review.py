"""LLM code reviewer gate entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

from infra.config import WORKSPACE
from infra.product_events import emit_product_event
from reviewer.reviewer_backends import (
    invoke_local_model,
    is_27b_loaded,
    write_calibration_data,
)
from reviewer.reviewer_categories import (
    LABEL_OVERRIDE,
    LABEL_REVIEW_LOCAL,
    LABEL_REVIEW_PARALLEL,
)
from reviewer.reviewer_events import emit_finding_events
from reviewer.reviewer_exact_source import exact_review_diff
from reviewer.reviewer_context import (
    ReviewerContextManifest,
    resolve_reviewer_context,
    reviewer_context_to_prompt_text,
    reviewer_standards_registry,
    reviewer_version_digest,
)
from reviewer.reviewer_parser import parse_findings
from reviewer.reviewer_runner import (
    _cc_model_for_base,
    configured_primary_backend,
    configured_primary_label,
    run_cc_review,
)
from prompts.reviewer import build_reviewer_prompt, reviewer_prompt_version_text
from reviewer.reviewer_prd_context import resolve_prd_contracts
from reviewer.pr_handoff import (
    _PR_HANDOFF_MAX_CHARS,
    _PR_HANDOFF_TITLE_MAX_CHARS,
    pr_handoff_context as _pr_handoff_context,
)
from reviewer.review_comment_fetch import (
    fetch_existing_issue_comments,
    fetch_existing_review_comments,
)
from reviewer.review_publication import prepare_findings_for_publication, publish_review
from reviewer.reviewer_comment import format_status_summary
from reviewer.reviewer_status import set_error, set_failure, set_pending, set_success
from reviewer.reviewer_subsystems import extract_changed_files_from_diff
from reviewer.reviewer_threads import (
    build_review_history,
    classify_review_findings,
)
from reviewer.reviewer_types import (
    ReviewComments,
    ReviewerResult,
    ReviewerVerdict,
    reviewer_verdict,
)

log = logging.getLogger(__name__)

_WORKSPACE_ROOT = str(WORKSPACE)
_PROJECT_ROOT = str(WORKSPACE / "projects" / "corsia" / "source" / "corsia")


# UX/IA remains a separate screenshot-based vision-audit modality.
_CODE_REVIEW_STANDARDS = ["development-doctrine", "eng-arch", "product", "copy"]


@dataclass(frozen=True)
class _ReviewContext:
    """Reviewer prompt context and its stable standards attribution."""

    text: str
    reviewer_version: str
    reviewer_standards: list[str]


def on_pr_review(event_type: str, payload: dict[str, Any]) -> ReviewerResult | None:
    """Run the LLM code reviewer for a PR-shaped payload."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    repo = payload.get("repository", {}).get("full_name", "")
    head_sha = pr.get("head", {}).get("sha", "")
    labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]

    if not pr_number or not repo:
        log.error("PR reviewer: missing pr_number or repo in payload")
        return None

    if LABEL_OVERRIDE in labels:
        log.info("PR #%d: %s label present, skipping review", pr_number, LABEL_OVERRIDE)
        summary = f"{LABEL_OVERRIDE} -- review skipped by override"
        # Structured ``skipped`` prevents a false clean owner verdict.
        if not set_success(repo, head_sha, summary):
            raise RuntimeError("failed to publish override reviewer status")
        return {
            "verdict": "skipped",
            "summary": summary,
            "model_label": LABEL_OVERRIDE,
        }

    mode = _detect_mode(labels)
    log.info("PR #%d: review mode=%s, sha=%s", pr_number, mode, head_sha[:8])

    emit_product_event(
        "reviewer", "started",
        properties={
            "pr_number": pr_number,
            "mode": mode,
            "model": _model_label_for_mode(mode),
            "backend": "omlx" if mode == "local" else configured_primary_backend(),
        },
    )

    start_time = time.monotonic()
    try:
        review_comments = fetch_existing_review_comments(pr_number, repo)
        issue_comments = fetch_existing_issue_comments(pr_number, repo)
        if review_comments is None or issue_comments is None:
            raise RuntimeError("review comment history is unreadable")
        prior = (review_comments, issue_comments)
        if mode == "parallel":
            return _run_parallel(pr_number, repo, head_sha, payload, prior)
        if mode == "local":
            return _run_local(pr_number, repo, head_sha, payload, prior)
        return _run_cc(pr_number, repo, head_sha, payload, prior)
    except Exception as exc:
        elapsed = int((time.monotonic() - start_time) * 1000)
        log.error("PR #%d: reviewer failed: %s", pr_number, exc)
        set_error(repo, head_sha, _error_status_text(exc))
        emit_product_event(
            "reviewer", "errored",
            properties={
                "pr_number": pr_number,
                "error": str(exc)[:200],
                "elapsed_ms": elapsed,
            },
        )
        emit_product_event(
            "reviewer", "failed",
            properties={
                "pr_number": pr_number,
                "error": str(exc)[:200],
                "retry_count": 0,
                "elapsed_ms": elapsed,
            },
        )
        # Infra failure is not a verdict; re-raise so the SHA remains retryable.
        raise


def _error_status_text(exc: Exception) -> str:
    """Keep the useful failure detail within GitHub's 140-character limit."""
    prefix = "qa/llm-reviewer: error — "
    return f"{prefix}{str(exc)[:140 - len(prefix)]}"


def _detect_mode(labels: list[str]) -> str:
    """Detect review mode from PR labels."""
    if LABEL_REVIEW_PARALLEL in labels:
        return "parallel"
    if LABEL_REVIEW_LOCAL in labels:
        return "local"
    return "cc"


def _model_label_for_mode(mode: str) -> str:
    """Return human-readable model label for the given mode (telemetry)."""
    return "qwen3.6-27B Dense" if mode == "local" else configured_primary_label()


def _base_ref(payload: dict[str, Any]) -> str:
    """Extract the PR base branch from the webhook payload."""
    return payload.get("pull_request", {}).get("base", {}).get("ref", "")


def _base_sha(payload: dict[str, Any]) -> str:
    """Extract the immutable PR base commit from the webhook payload."""
    return payload.get("pull_request", {}).get("base", {}).get("sha", "")


def _review_diff(payload: dict[str, Any], head_sha: str) -> str:
    """Read the one immutable diff used by every reviewer input path."""
    return exact_review_diff(_PROJECT_ROOT, _base_sha(payload), head_sha)


def _run_cc(
    pr_number: int, repo: str, head_sha: str,
    payload: dict[str, Any],
    prior: ReviewComments,
) -> ReviewerVerdict | None:
    """Run the configured cloud reviewer."""
    base_ref = _base_ref(payload)
    _, label = _cc_model_for_base(base_ref)
    set_pending(repo, head_sha, f"Review in progress ({label})")

    diff = _review_diff(payload, head_sha)

    context = _build_context(diff, pr_number, repo, payload=payload)
    result = run_cc_review(
        diff, context.text, pr_number, repo, head_sha,
        working_dir=_PROJECT_ROOT, base_ref=base_ref,
        base_sha=_base_sha(payload),
        review_history=build_review_history(prior[0] + prior[1]),
    )
    if result is None:
        return None
    raw_output, label_used = result

    return _process_findings(
        raw_output, pr_number, repo, head_sha, label_used,
        fallback_file=next(iter(extract_changed_files_from_diff(diff)), ""),
        reviewer_version=context.reviewer_version,
        reviewer_standards=context.reviewer_standards,
    )


def _run_local(
    pr_number: int, repo: str, head_sha: str,
    payload: dict[str, Any],
    prior: ReviewComments,
) -> ReviewerVerdict | None:
    """Run the review using local oMLX qwen3.6-27B Dense."""
    if not is_27b_loaded():
        log.error("PR #%d: qwen3.6-27B Dense not loaded in oMLX", pr_number)
        set_failure(repo, head_sha, "qa/llm-reviewer: 27B-not-loaded")
        emit_product_event("reviewer", "failed", properties={"pr_number": pr_number, "error": "27B-not-loaded", "retry_count": 0})
        return None

    set_pending(repo, head_sha, "Review in progress (qwen3.6-27B Dense)")
    diff = _review_diff(payload, head_sha)

    context = _build_context(diff, pr_number, repo, payload=payload)
    prompt = build_reviewer_prompt(
        diff, context.text, model_label="qwen3.6-27B Dense",
        review_history=build_review_history(prior[0] + prior[1]),
    )
    raw_output = invoke_local_model(prompt)
    return _process_findings(
        raw_output, pr_number, repo, head_sha, "qwen3.6-27B Dense",
        fallback_file=next(iter(extract_changed_files_from_diff(diff)), ""),
        reviewer_version=context.reviewer_version,
        reviewer_standards=context.reviewer_standards,
    )


def _run_parallel(
    pr_number: int, repo: str, head_sha: str,
    payload: dict[str, Any],
    prior: ReviewComments,
) -> ReviewerVerdict | None:
    """Run both CC and local; write calibration data. CC is trusted gate."""
    base_ref = _base_ref(payload)
    _, cc_label = _cc_model_for_base(base_ref)
    set_pending(repo, head_sha, f"Review in progress (parallel: {cc_label} + 27B)")

    diff = _review_diff(payload, head_sha)

    context = _build_context(diff, pr_number, repo, payload=payload)

    cc_result = run_cc_review(
        diff, context.text, pr_number, repo, head_sha,
        working_dir=_PROJECT_ROOT, base_ref=base_ref,
        base_sha=_base_sha(payload),
        review_history=build_review_history(prior[0] + prior[1]),
    )

    local_output = None
    if is_27b_loaded():
        local_prompt = build_reviewer_prompt(
            diff, context.text, model_label="qwen3.6-27B Dense",
            review_history=build_review_history(prior[0] + prior[1]),
        )
        try:
            local_output = invoke_local_model(local_prompt)
        except Exception as exc:
            log.warning("PR #%d: local 27B review failed: %s", pr_number, exc)
    else:
        log.info("PR #%d: 27B not loaded, skipping local leg", pr_number)

    write_calibration_data(
        pr_number,
        cc_result[0] if cc_result else None,
        local_output,
        cc_label=cc_result[1] if cc_result else cc_label,
    )

    if cc_result is not None:
        return _process_findings(
            cc_result[0], pr_number, repo, head_sha, cc_result[1],
            fallback_file=next(iter(extract_changed_files_from_diff(diff)), ""),
            reviewer_version=context.reviewer_version,
            reviewer_standards=context.reviewer_standards,
        )
    return None


def _process_findings(
    raw_output: str, pr_number: int, repo: str, head_sha: str, model_label: str,
    *,
    fallback_file: str = "",
    reviewer_version: str,
    reviewer_standards: list[str],
) -> ReviewerVerdict:
    """Parse findings, dedup against prior review state, and post updates."""
    raw_findings = parse_findings(raw_output)
    findings = prepare_findings_for_publication(raw_findings, fallback_file=fallback_file)
    required_action, summary, block_count, flag_count, nit_count = format_status_summary(
        findings, model_label,
    )
    emit_finding_events(
        emit_product_event, raw_findings, pr_number, reviewer_version, reviewer_standards,
    )

    # Publication uses a fresh snapshot after the potentially long model call;
    # the pre-run snapshot exists only to give the model bounded trajectory.
    review_comments = fetch_existing_review_comments(pr_number, repo)
    issue_comments = fetch_existing_issue_comments(pr_number, repo)
    if review_comments is None or issue_comments is None:
        raise RuntimeError("review comment history is unreadable")

    # Both finding channels participate in deduplication and resolution.
    comments = review_comments + issue_comments
    new_threads, resolve_replies, unchanged_count = classify_review_findings(
        raw_findings, findings, comments, pr_number, repo, head_sha,
        approach_reset=required_action == "approach_reset",
    )

    publish_review(
        pr_number=pr_number,
        repo=repo,
        head_sha=head_sha,
        findings=findings,
        new_threads=new_threads,
        resolve_replies=resolve_replies,
        unchanged_count=unchanged_count,
        model_label=model_label,
        existing_issue_comments=issue_comments,
    )

    verdict = "code_fail" if block_count > 0 else "clean"
    if verdict == "code_fail":
        # A real verdict is senior to a standing success waiver.
        status_published = set_failure(repo, head_sha, summary, verdict=True)
    else:
        status_published = set_success(repo, head_sha, summary)
    if not status_published:
        raise RuntimeError(f"failed to publish {verdict} reviewer status")

    emit_product_event(
        "reviewer", "completed",
        properties={
            "pr_number": pr_number,
            "finding_count": len(findings),
            "block_count": block_count,
            "new_count": len(new_threads),
            "unchanged_count": unchanged_count,
            "resolved_count": len(resolve_replies),
            "reviewer_version": reviewer_version,
            "reviewer_standards": reviewer_standards,
        },
    )
    return reviewer_verdict(
        verdict=verdict,
        required_action=required_action,
        summary=summary,
        block_count=block_count,
        flag_count=flag_count,
        nit_count=nit_count,
        finding_count=len(findings),
        model_label=model_label,
        reviewer_version=reviewer_version,
        reviewer_standards=reviewer_standards,
    )


def _build_context(
    diff: str,
    pr_number: int,
    repo: str,
    *,
    payload: dict[str, Any] | None = None,
) -> _ReviewContext:
    """Build prompt context and version from one resolved standards snapshot."""
    manifest = ReviewerContextManifest(anti_patterns=True, standards=_CODE_REVIEW_STANDARDS)
    standards = resolve_reviewer_context(manifest, _WORKSPACE_ROOT)
    _require_standards(standards)
    standards_text = reviewer_context_to_prompt_text(standards)
    reviewer_standards = _resolved_standard_keys(standards)
    resolved = [*standards, *_resolve_prd_contracts(diff, pr_number, repo)]
    context_text = reviewer_context_to_prompt_text(resolved)
    handoff = _pr_handoff_context(payload)
    return _ReviewContext(
        text=f"{context_text}\n\n{handoff}" if handoff else context_text,
        reviewer_version=reviewer_version_digest(reviewer_prompt_version_text(), standards_text),
        reviewer_standards=reviewer_standards,
    )


def _resolved_standard_keys(resolved: list[tuple[str, str]]) -> list[str]:
    """Return registry keys represented by the resolved standards entries."""
    registry = reviewer_standards_registry()
    return [
        key for key, relative_path in registry.items()
        if any(path.endswith(relative_path) for path, _ in resolved)
    ]


def _resolve_prd_contracts(
    diff: str, pr_number: int, repo: str,
) -> list[tuple[str, str]]:
    """Resolve approved PRD contracts and report non-blocking coverage gaps."""
    contracts = resolve_prd_contracts(
        diff, pr_number, repo, _WORKSPACE_ROOT, emit=emit_product_event,
    )
    if not contracts:
        log.info("reviewer: PR #%d — no approved PRD contract for changed paths", pr_number)
    return contracts


def _require_standards(resolved: list[tuple[str, str]]) -> None:
    """Fail loudly if an advertised always-on reviewer input is absent or empty."""
    registry = reviewer_standards_registry()
    required = [*_CODE_REVIEW_STANDARDS, "anti-patterns"]
    if unknown := [key for key in required if key not in registry]:
        raise RuntimeError(
            f"reviewer config error: standards key(s) {unknown} have no entry in "
            "the standards registry — reconcile _CODE_REVIEW_STANDARDS with "
            "_STANDARDS_REGISTRY in reviewer_context.py"
        )

    missing = [
        key
        for key in required
        if not any(registry[key] in path and content.strip() for path, content in resolved)
    ]
    if missing:
        wanted = [registry[k] for k in missing]
        raise RuntimeError(
            f"reviewer context is missing or empty for required inputs {missing}; "
            "the reviewer must not run without its standards contracts and anti-pattern catalog — "
            f"verify these exist and are non-empty in the workspace: {wanted}"
        )
