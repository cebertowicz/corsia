"""Pluggable pane grouping providers.

A provider projects a presentation group (e.g. a Git worktree) over artifact
items without creating a synthetic persisted artifact. Providers return group
identity, label, and order only — never retention, surfacing, authorization,
or artifact state. Git is the day-zero built-in provider; a future connector
registers its own provider without touching pane control flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from infra.project_repo_policy import RepoPolicy
from tools.sandbox_config import (
    registered_repo_for_path,
    repo_worktree_branch,
    repo_worktree_root,
)


@dataclass(frozen=True)
class GroupAssignment:
    """One provider's group placement for a single artifact path."""

    provider: str
    key: str
    label: str
    branch: str


class GroupingProvider(Protocol):
    name: str

    def group_for(
        self, path: str, *, policies: list[RepoPolicy], cache: dict
    ) -> GroupAssignment | None:
        """Return this path's group, or None if this provider doesn't claim it."""
        ...


class GitWorktreeGroupingProvider:
    """Groups artifacts under their verified linked-worktree root.

    Resolution: ``registered_repo_for_path`` finds the containing registered
    repository, then the existing reciprocal-gitdir-verified
    ``repo_worktree_root`` confirms the path sits inside a genuine linked
    worktree (never a symlink/path spoof). A file directly in a repo's base
    checkout (not under ``.worktrees/``) is not grouped by this provider.
    """

    name = "git-worktree"

    def group_for(
        self, path: str, *, policies: list[RepoPolicy], cache: dict
    ) -> GroupAssignment | None:
        if not path or "://" in path:
            return None
        item_path = Path(path)
        repo = registered_repo_for_path(item_path, policies=policies, cache=cache)
        if repo is None:
            return None
        worktree_root = repo_worktree_root(item_path, repo.path, cache=cache)
        if worktree_root is None:
            return None

        branch_key = ("branch", str(worktree_root))
        if branch_key not in cache:
            branch_info = repo_worktree_branch(worktree_root, repo.path, cache=cache)
            cache[branch_key] = (
                branch_info[1]
                if branch_info is not None and branch_info[1]
                else worktree_root.name
            )
        branch = cache[branch_key]

        return GroupAssignment(
            provider=self.name,
            key=str(worktree_root),
            label=f"{branch} · {worktree_root.name}",
            branch=branch,
        )


_PROVIDERS: list[GroupingProvider] = [GitWorktreeGroupingProvider()]


def registered_providers() -> list[GroupingProvider]:
    """Return the ordered active grouping providers (Git built-in first)."""
    return list(_PROVIDERS)


def register_provider(provider: GroupingProvider) -> None:
    """Register an additional grouping provider (e.g. a future connector)."""
    _PROVIDERS.append(provider)


def group_for_path(
    path: str | None, *, policies: list[RepoPolicy], cache: dict
) -> GroupAssignment | None:
    """Return the first claiming provider's group for ``path``, or None.

    ``cache`` is caller-owned and must be created fresh per pane request:
    resolution is cached per distinct path/repo-root pair for that request
    only (design risk: "Derived Git checks are costly").
    """
    if not path:
        return None
    for provider in _PROVIDERS:
        assignment = provider.group_for(path, policies=policies, cache=cache)
        if assignment is not None:
            return assignment
    return None
