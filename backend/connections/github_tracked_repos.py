"""Which GitHub repositories this instance tracks, from its registered source repositories.

The one place the answer is computed. The GitHub read broker (what a read may
reach) and the notifications feed (which notifications to keep) both read it
from here, so neither imports the other.
"""

from __future__ import annotations

from typing import Any

__all__ = ["RepoScopeUnavailable", "projects_tracking_github_repository", "tracked_github_repositories"]


class RepoScopeUnavailable(RuntimeError):
    """The registered source repositories could not be read."""


def _remotes(policy: Any) -> set[str]:
    """The GitHub repositories one registered source repository's remotes name."""
    from infra.worktree_reaper_probe import github_remote_full_name

    if not policy.source_binding_valid or policy.enforcement_error:
        return set()
    return {repository for remote in ("origin", "upstream")
            if (repository := github_remote_full_name(policy.path, remote)) is not None}


def _policies(project_id: str | None = None) -> list[Any]:
    from infra.project_repo_policy import RepoPolicyQueryError, list_registered_repo_policies

    try:
        return (list_registered_repo_policies(project_id=project_id) if project_id
                else list_registered_repo_policies())
    except RepoPolicyQueryError as exc:
        raise RepoScopeUnavailable from exc


def tracked_github_repositories(project_id: str | None = None) -> frozenset[str]:
    """Resolve tracked GitHub repositories from the registered source repositories,
    of one project when ``project_id`` is given."""
    from infra.worktree_reaper_probe import ReaperGitError

    try:
        return frozenset(repo for policy in _policies(project_id) for repo in _remotes(policy))
    except ReaperGitError as exc:
        raise RepoScopeUnavailable from exc


def projects_tracking_github_repository(repository: str) -> frozenset[str]:
    """The inverse: the projects whose registered source repositories track
    ``repository``. Bounded by the registered repositories, never by any rule."""
    from infra.worktree_reaper_probe import ReaperGitError

    wanted = repository.lower()
    try:
        return frozenset(policy.project_id for policy in _policies()
                         if wanted in _remotes(policy))
    except ReaperGitError as exc:
        raise RepoScopeUnavailable from exc
