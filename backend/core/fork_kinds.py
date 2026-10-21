"""The single registry of concrete disposable fork policies."""

from __future__ import annotations

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.fork_runtime import ForkKind


FORK_KINDS: dict[str, ForkKind] = {
    CHECKPOINT_KIND.name: CHECKPOINT_KIND,
    ARTIFACT_REVIEW_KIND.name: ARTIFACT_REVIEW_KIND,
}


def require_fork_kind(name: str) -> ForkKind:
    """Resolve a persisted kind name or report an unknown kind."""
    try:
        return FORK_KINDS[name]
    except KeyError as error:
        raise ValueError(f"unknown fork kind: {name}") from error
