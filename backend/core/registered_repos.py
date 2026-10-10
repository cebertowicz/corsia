"""Registered-repository rendering for project bootloader context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from infra.project_repo_policy import RepoPolicy


def format_registered_repo_policy(name: str, policy: RepoPolicy) -> str:
    """Return one canonical ``name (path) pinned=… protected`` display line.

    Single source of truth for how a registered repository is rendered to
    agents, shared by the ``project_repos`` tool and the project bootloader
    context so the two surfaces cannot diverge.
    """
    line = f"{name} ({policy.path})"
    if policy.pinned_branch:
        line += f" pinned={policy.pinned_branch}"
    if policy.protect_base_checkout:
        line += " protected"
    return line


def registered_repo_context_lines(
    project_id: str,
    *,
    db=None,
) -> list[str]:
    """Return context lines for the project's registered repositories.

    These bytes sit in the cache-stable prefix, so they are a pure function of
    project state: the same project renders the same block every turn, and the
    block changes only when the registered repositories actually change. A
    failed policy lookup therefore raises rather than rendering a second,
    different string — silently swapping in "unavailable" would both disguise
    a multi-repo project as single-repo and invalidate the prefix cache on a
    transient error. Unreadable required project context already fails loudly
    here (see ``_loaded_project_context_files``); this matches it.
    """
    policies = _registered_repo_policies(project_id, db=db)

    lines = []
    if policies:
        lines.append("Registered repositories:")
        lines.extend(
            f"- {format_registered_repo_policy(name, policies[name])}"
            for name in sorted(policies)
        )
    return lines


def _registered_repo_policies(project_id: str, *, db=None) -> dict[str, RepoPolicy]:
    """Return the project's registered repo policies, or raise on failure."""
    from infra.project_repo_policy import get_project_repo_policies

    try:
        return get_project_repo_policies(project_id, db=db)
    except Exception as exc:
        logger.warning(
            "registered repo policy lookup failed for project {}: {}",
            project_id,
            exc,
        )
        raise RuntimeError(
            f"registered repository policy is unavailable for project "
            f"{project_id}: {exc}"
        ) from exc
