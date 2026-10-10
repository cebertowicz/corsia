"""The ``pr_merged`` trigger for the worktree reaper.

The router's source-fact step is synchronous and must never wait on Git or
see a reaper error, so ``schedule_merged_pr_reap`` starts one ``to_thread``
task, kept in a module set until it finishes. There is no loop: one merge,
one task. Every outcome, including "nothing mapped", emits the run metric.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from infra.project_repo_policy import RepoPolicy, list_registered_repo_policies
from infra.worktree_reaper import RepoReport, emit_report, failed_report, reap
from infra.worktree_reaper_probe import (
    ReaperGitError,
    is_ancestor,
    list_worktrees,
    origin_full_name,
)
from tools.sandbox_config import repo_worktree_root

_PENDING: set[asyncio.Task[None]] = set()


def _policy_for(full_name: str, policies: list[RepoPolicy]) -> RepoPolicy | None:
    for policy in policies:
        if not policy.source_binding_valid or policy.enforcement_error:
            continue
        try:
            if origin_full_name(policy.path) == full_name.lower():
                return policy
        except ReaperGitError:
            continue
    return None


def _mapped_worktree(
    repo: Path, branch: str, pr_sha: str, report: RepoReport,
) -> Path | None:
    matches = [
        entry for entry in list_worktrees(repo)
        if entry.branch == f"refs/heads/{branch}"
    ]
    entry = matches[0] if len(matches) == 1 else None
    if entry is None or repo_worktree_root(entry.path, repo) != entry.path:
        report.skip(branch, None, "no_worktree")
        return None
    if entry.head is None or not is_ancestor(repo, entry.head, pr_sha):
        report.skip(entry.path.name, None, "ahead_of_pr")
        return None
    return entry.path


def reap_merged_pr(
    payload: dict[str, Any], *, policies: list[RepoPolicy] | None = None,
) -> RepoReport:
    """Reap the worktree mapped to one merged PR, if it is safe to."""
    repository = payload.get("repository") or {}
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    full_name = str(repository.get("full_name") or "")
    branch, pr_sha = head.get("ref"), head.get("sha")
    report = RepoReport(repo=full_name, trigger="pr_terminal")
    if (head.get("repo") or {}).get("full_name") != full_name:
        report.skip(str(branch), None, "fork")
        return report
    if not (full_name and isinstance(branch, str) and isinstance(pr_sha, str)):
        report.skip(str(branch), None, "payload_incomplete")
        return report
    loaded = policies if policies is not None else list_registered_repo_policies()
    policy = _policy_for(full_name, loaded)
    if policy is None:
        report.skip(branch, None, "no_repo")
        return report
    repo = policy.path.resolve(strict=False)
    report.repo = str(repo)
    worktree = _mapped_worktree(repo, branch, pr_sha, report)
    if worktree is None:
        return report
    return reap(policy, mode="pr_terminal", candidates=[worktree], pr_sha=pr_sha)


def _reap_merged_pr_safely(payload: dict[str, Any]) -> None:
    try:
        report = reap_merged_pr(payload)
    except Exception as exc:
        logger.exception("worktree_reaper: merged-PR pass failed")
        repo = str((payload.get("repository") or {}).get("full_name") or "")
        report = failed_report(repo, "pr_terminal", "pass_error", exc)
    try:
        emit_report(report)
    except Exception:
        logger.exception("worktree_reaper: metric emit failed")


def schedule_merged_pr_reap(payload: dict[str, Any]) -> None:
    """Start one background reap for a merged PR; never blocks or raises."""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(asyncio.to_thread(_reap_merged_pr_safely, payload))
    except Exception:
        logger.exception("worktree_reaper: could not schedule merged-PR pass")
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
