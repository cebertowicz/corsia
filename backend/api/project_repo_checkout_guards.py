"""Checkout guards and retryable Git failures for repository policy changes."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from infra.project_repo_policy import (
    RepoCheckoutState,
    RepoGitTimeout,
    branch_pin_support_error,
    repo_checkout_state,
)


def repo_git_timeout_response(exc: RepoGitTimeout) -> HTTPException:
    """Return the retryable API response for a transient Git timeout."""
    return HTTPException(
        503,
        f"Repository Git operation is temporarily unavailable: {exc}. "
        "Retry the request.",
    )


def require_supported_pin(
    repo_path: Path,
    support_check=branch_pin_support_error,
) -> None:
    """Reject unsupported pinning or surface a retryable Git timeout."""
    try:
        pin_error = support_check(repo_path)
    except RepoGitTimeout as exc:
        raise repo_git_timeout_response(exc) from exc
    if pin_error:
        raise HTTPException(409, f"Repository cannot be pinned safely: {pin_error}")


def capture_clean_checkout(repo_path: Path) -> RepoCheckoutState:
    """Capture a clean checkout or raise the appropriate policy-update response."""
    try:
        checkout = repo_checkout_state(repo_path)
    except RepoGitTimeout as exc:
        raise repo_git_timeout_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            409,
            f"Repository checkout could not be verified: {exc}",
        ) from exc
    if not checkout.clean:
        raise HTTPException(
            409,
            "Repository checkout must be clean before changing its policy",
        )
    return checkout


def require_checkout_unchanged(
    repo_path: Path,
    expected: RepoCheckoutState,
) -> None:
    """Require a policy-update checkout to retain its verified clean identity."""
    try:
        current = repo_checkout_state(repo_path)
    except RepoGitTimeout as exc:
        raise repo_git_timeout_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            409,
            f"Repository checkout changed during the policy update: {exc}",
        ) from exc
    if current != expected or not current.clean:
        raise HTTPException(
            409,
            "Repository checkout changed during the policy update",
        )
