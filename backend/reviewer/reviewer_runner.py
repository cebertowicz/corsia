"""Configured reviewer execution policy."""

from __future__ import annotations

import logging
import os
import time

from infra.product_events import emit_product_event
from infra.settings import get_agent_defaults
from prompts.reviewer import build_reviewer_prompt
from reviewer.reviewer_backends import (
    CCRateLimitError,
    CCUnavailableError,
    invoke_codex_reviewer,
    invoke_cc_subagent,
)
from reviewer.reviewer_account import reviewer_launch
from reviewer.reviewer_categories import MAX_RETRIES, RETRY_BACKOFF_MINUTES
from reviewer.reviewer_exact_source import exact_review_source
from reviewer.reviewer_status import set_failure, set_pending

log = logging.getLogger(__name__)

_PRIMARY_ENV = "CORSIA_REVIEWER_MODEL"
_PRIMARY_SETTING = "reviewer_model"
_FALLBACK_ENV = "CORSIA_REVIEWER_FALLBACK_MODEL"
_FALLBACK_SETTING = "reviewer_fallback_model"
REVIEWER_HARNESSES = ("codex", "claude-code")


def reviewer_model_error(model_id: object) -> str | None:
    """Validate an exact catalog ID against the reviewer's supported harnesses."""
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    if not isinstance(model_id, str):
        return "That model isn't in the catalog. Choose another model."
    try:
        target = resolve_execution_target(model_id)
    except (ModelUnavailableError, ValueError):
        return "That model isn't in the catalog. Choose another model."
    if target.catalog_id != model_id:
        return "That model isn't in the catalog. Choose another model."
    if target.harness not in REVIEWER_HARNESSES:
        return "The reviewer needs a Codex or Claude Code model. Choose another model."
    return None


def _resolve_model(env_var: str, setting_key: str) -> str | None:
    """env var → ``agents.defaults.<setting_key>`` → None. No literal default."""
    env_val = os.environ.get(env_var)
    if env_val and env_val.strip():
        return env_val.strip()
    configured = get_agent_defaults().get(setting_key)
    if configured and str(configured).strip():
        return str(configured).strip()
    return None


def _resolve_reviewer_target(model: str):
    """Resolve legacy bare CC settings and canonical reviewer selections."""
    from llm.resolution import (
        ModelUnavailableError,
        resolve_execution_target,
        resolve_harness_selector,
    )

    try:
        try:
            return resolve_execution_target(model)
        except ModelUnavailableError:
            return resolve_harness_selector("claude-code", model)
    except (ModelUnavailableError, ValueError) as exc:
        raise CCUnavailableError(
            f"Reviewer model {model!r} cannot be resolved: {exc}"
        ) from exc


def _resolve_for_base(model: str, base_ref: str):
    """Resolve the configured model and any explicit main-base CC variant."""
    target = _resolve_reviewer_target(model)
    selected_model = model
    if (
        base_ref == "main"
        and target.harness == "claude-code"
        and not target.transport_model.endswith("]")
    ):
        selected_model = f"{model}[1m]"
        target = _resolve_reviewer_target(f"{target.transport_model}[1m]")
        if target.harness != "claude-code":
            raise CCUnavailableError(
                f"Reviewer model {selected_model!r} resolved to {target.harness}, "
                "not claude-code"
            )
    return selected_model, target


def _backend_for_target(target) -> str:
    """Reviewer backend name for one resolved execution target."""
    return "codex" if target.harness == "codex" else "cc"


def _backend_for_model(model: str) -> str:
    """Reviewer backend name for telemetry and invocation routing."""
    return _backend_for_target(_resolve_reviewer_target(model))


def _label(model: str, *, is_fallback: bool) -> str:
    """Human label for the posted review."""
    base = model
    parts: list[str] = []
    if base.endswith("[1m]"):
        base = base[: -len("[1m]")]
        parts.append("1M")
    if is_fallback:
        parts.append("fallback")
    return f"{base} ({', '.join(parts)})" if parts else base


def _primary_model() -> str:
    """Return the configured primary reviewer model or fail with guidance."""
    model = _resolve_model(_PRIMARY_ENV, _PRIMARY_SETTING)
    if not model:
        raise RuntimeError(
            f"No reviewer model configured. Set the {_PRIMARY_ENV} env var "
            f"or `agents.defaults.{_PRIMARY_SETTING}` in settings.json. "
            "CORSIA never falls back to a hardcoded default model."
        )
    return model


def _cc_model_for_base(base_ref: str) -> tuple[str, str]:
    """Return ``(model_id, display_label)`` for the primary reviewer model."""
    model = _primary_model()
    try:
        selected, _ = _resolve_for_base(model, base_ref)
    except CCUnavailableError:
        # Label-only callers must not bypass run_cc_review's fallback disposition.
        selected = model
    return selected, _label(selected, is_fallback=False)


def _cc_fallback_for_base(base_ref: str) -> tuple[str, str] | None:
    """Return ``(model_id, display_label)`` for the fallback reviewer model."""
    model = _resolve_model(_FALLBACK_ENV, _FALLBACK_SETTING)
    if not model:
        return None
    try:
        selected, _ = _resolve_for_base(model, base_ref)
    except CCUnavailableError:
        selected = model
    return selected, _label(selected, is_fallback=True)


def configured_primary_label() -> str:
    """Best-effort label of the configured primary reviewer model."""
    return _resolve_model(_PRIMARY_ENV, _PRIMARY_SETTING) or "configured reviewer"


def configured_primary_backend() -> str:
    """Best-effort reviewer backend label for telemetry."""
    model = _resolve_model(_PRIMARY_ENV, _PRIMARY_SETTING)
    if not model:
        return "unknown"
    try:
        return _backend_for_model(model)
    except Exception:
        return "unknown"


def run_cc_review(
    diff: str,
    context_text: str,
    pr_number: int,
    repo: str,
    head_sha: str,
    working_dir: str,
    base_ref: str,
    base_sha: str = "",
    review_history: str = "",
) -> tuple[str, str] | None:
    """Run the reviewer with rate-limit retries and configured fallback."""
    model = _primary_model()
    label = _label(model, is_fallback=False)
    try:
        model, target = _resolve_for_base(model, base_ref)
        label = _label(model, is_fallback=False)
        out = _attempt(
            diff, context_text, pr_number, repo, head_sha, working_dir,
            model, label, base_sha, review_history, target=target,
        )
        return (out, label) if out is not None else None
    except CCUnavailableError as exc:
        fb_model = _resolve_model(_FALLBACK_ENV, _FALLBACK_SETTING)
        if not fb_model:
            _fail_unavailable(repo, head_sha, pr_number, exc, "model-unavailable-no-fallback")
            return None

        fb_label = _label(fb_model, is_fallback=True)
        fb_target = None
        fb_resolution_error = None
        try:
            fb_model, fb_target = _resolve_for_base(fb_model, base_ref)
            fb_label = _label(fb_model, is_fallback=True)
        except CCUnavailableError as fallback_exc:
            fb_resolution_error = fallback_exc
        log.warning(
            "PR #%d: %s unavailable (%s) — falling back to %s",
            pr_number, label, str(exc)[:120], fb_label,
        )
        set_pending(repo, head_sha, f"{label} unavailable — retrying with {fb_label}")
        emit_product_event(
            "reviewer", "fallback",
            properties={
                "pr_number": pr_number,
                "from_model": model,
                "to_model": fb_model,
                "reason": str(exc)[:200],
            },
        )

        if fb_resolution_error is not None:
            _fail_unavailable(
                repo, head_sha, pr_number, fb_resolution_error,
                "model-unavailable-after-fallback",
            )
            return None

    try:
        assert fb_target is not None
        out = _attempt(
            diff, context_text, pr_number, repo, head_sha, working_dir,
            fb_model, fb_label, base_sha, review_history, target=fb_target,
        )
        return (out, fb_label) if out is not None else None
    except CCUnavailableError as exc:
        _fail_unavailable(repo, head_sha, pr_number, exc, "model-unavailable-after-fallback")
        return None


def _attempt(
    diff: str, context_text: str, pr_number: int, repo: str,
    head_sha: str, working_dir: str, model: str, label: str,
    base_sha: str, review_history: str,
    *, target=None,
) -> str | None:
    """Run one review attempt on ``model``."""
    with exact_review_source(
        working_dir, base_sha, head_sha,
    ) as (change_source, review_dir, source_git_dir):
        prompt = build_reviewer_prompt(
            diff,
            context_text,
            model_label=label,
            change_source=change_source,
            review_history=review_history,
        )
        return _run_cc_with_retries(
            prompt, pr_number, repo, head_sha, review_dir,
            model=model, target=target, source_git_dir=source_git_dir,
            base_sha=base_sha,
        )


def _fail_unavailable(
    repo: str, head_sha: str, pr_number: int, exc: Exception, reason: str,
) -> None:
    """Set the reviewer check to failure for a model-unavailable outcome."""
    log.error("PR #%d: reviewer model-unavailable (%s): %s", pr_number, reason, exc)
    set_failure(repo, head_sha, f"qa/llm-reviewer: model-unavailable — {str(exc)[:60]}")
    emit_product_event(
        "reviewer", "failed",
        properties={"pr_number": pr_number, "error": reason, "retry_count": 0},
    )


def _run_cc_with_retries(
    prompt: str, pr_number: int, repo: str,
    head_sha: str, working_dir: str, *, model: str, source_git_dir: str,
    base_sha: str, target=None,
) -> str | None:
    """Run the configured reviewer backend with exponential backoff retry."""
    target = target or _resolve_reviewer_target(model)
    backend = _backend_for_target(target)
    for attempt in range(MAX_RETRIES + 1):
        try:
            return _invoke_reviewer(
                prompt, working_dir, target=target,
                source_git_dir=source_git_dir,
                base_sha=base_sha, head_sha=head_sha,
            )
        except CCRateLimitError:
            if attempt >= MAX_RETRIES:
                log.error(
                    "PR #%d: %s reviewer rate limit — all %d retries exhausted",
                    pr_number, backend, MAX_RETRIES,
                )
                set_failure(
                    repo, head_sha,
                    f"qa/llm-reviewer: {backend}-unavailable-after-retries",
                )
                emit_product_event(
                    "reviewer", "failed",
                    properties={
                        "pr_number": pr_number,
                        "error": f"{backend}-unavailable-after-retries",
                        "retry_count": attempt + 1,
                    },
                )
                return None

            wait_minutes = RETRY_BACKOFF_MINUTES[attempt]
            log.warning(
                "PR #%d: %s reviewer rate limited (attempt %d/%d), waiting %dm",
                pr_number, backend, attempt + 1, MAX_RETRIES,
                wait_minutes,
            )
            set_pending(repo, head_sha, f"Rate limited — retry {attempt + 1}/{MAX_RETRIES} in {wait_minutes}m")
            time.sleep(wait_minutes * 60)

    return None  # Should not reach here


def _invoke_reviewer(
    prompt: str, working_dir: str, *, target, source_git_dir: str,
    base_sha: str, head_sha: str,
) -> str:
    """Dispatch one model call through the configured reviewer backend."""
    if not target.available:
        raise CCUnavailableError(
            f"Reviewer model {target.catalog_id} is unavailable: "
            f"{target.availability_reason or 'unknown'}"
        )
    if target.harness not in REVIEWER_HARNESSES:
        raise CCUnavailableError(
            f"Reviewer model {target.catalog_id} is not an external harness model"
        )
    with reviewer_launch(target) as env:
        if target.harness == "codex":
            return invoke_codex_reviewer(
                prompt, working_dir, model=target.transport_model,
                source_git_dir=source_git_dir, env=env,
            )
        return invoke_cc_subagent(
            prompt,
            working_dir,
            model=target.transport_model,
            source_git_dir=source_git_dir,
            base_sha=base_sha,
            head_sha=head_sha,
            env=env,
        )
