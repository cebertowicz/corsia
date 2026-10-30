"""Fresh remote-based worktree creation for registered project repositories."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from infra.backend_git import git_argv
from infra.product_events import emit_product_event
from infra.project_repo_policy import RepoPolicy, list_registered_repo_policies
from tools.base import Tool

_DESCRIPTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorktreeCreateError(ValueError):
    """A bounded model-visible worktree creation failure."""


def _git(
    repo: Path,
    args: list[str],
    *,
    operation: str,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    """Run one direct Git command with repository hooks disabled."""
    try:
        result = subprocess.run(
            git_argv(*args),
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeCreateError(f"{operation} failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:240]
        raise WorktreeCreateError(
            f"{operation} failed: {detail or 'git failed'}"
        )
    return result


def _validate_branch(repo: Path, branch: str, *, label: str) -> str:
    value = branch.strip()
    if not value:
        raise WorktreeCreateError(f"{label} is required")
    _git(
        repo,
        ["check-ref-format", "--branch", value],
        operation=f"{label} validation",
    )
    return value


def _project_policy(project_id: str, repo_name: str) -> RepoPolicy:
    project_policies = list_registered_repo_policies(project_id=project_id)
    matches = [
        policy
        for policy in project_policies
        if policy.repo_name == repo_name
    ]
    # ``project_repo_policies`` is keyed PRIMARY KEY (project_id, repo_name),
    # so one project cannot hold two rows under one name: uniqueness is the
    # storage constraint's job, not a branch here.
    if not matches:
        valid = sorted(
            {policy.repo_name for policy in project_policies}
        )
        if valid:
            raise WorktreeCreateError(
                f"registered repository '{repo_name}' is unavailable in this "
                f"project; valid repositories: {', '.join(valid)}"
            )
        raise WorktreeCreateError(
            "no repositories are registered to this project; register a "
            "source repository with project_repo_register or in Project "
            "settings before creating a worktree"
        )
    policy = matches[0]
    if not policy.source_binding_valid or policy.enforcement_error:
        raise WorktreeCreateError(
            f"registered repository '{repo_name}' policy is not enforceable"
        )
    if not policy.path.is_dir():
        raise WorktreeCreateError(
            f"registered repository '{repo_name}' checkout is missing"
        )
    return policy


def _validate_worktree_request(
    *, thread_id: str, descriptor: str, remote: str
) -> None:
    """Reject a request whose identifiers cannot name a worktree."""
    if not _ID_RE.fullmatch(thread_id):
        raise WorktreeCreateError("current thread identity cannot name a worktree")
    if not _DESCRIPTOR_RE.fullmatch(descriptor):
        raise WorktreeCreateError(
            "descriptor must use 1-64 letters, numbers, dots, underscores, or dashes"
        )
    if not _REMOTE_RE.fullmatch(remote):
        raise WorktreeCreateError(
            "remote must use 1-64 letters, numbers, dots, underscores, or dashes"
        )


def _resolve_branch_and_base(
    repo: Path,
    policy: RepoPolicy,
    branch: str,
    base_branch: str | None,
) -> tuple[str, str]:
    """Validate the new branch and resolve its base against the repo policy."""
    branch = _validate_branch(repo, branch, label="branch")
    if policy.pinned_branch is not None:
        if base_branch is not None and base_branch != policy.pinned_branch:
            raise WorktreeCreateError(
                f"base_branch must match pinned branch '{policy.pinned_branch}'"
            )
        selected_base = policy.pinned_branch
    else:
        if base_branch is None:
            raise WorktreeCreateError(
                "base_branch is required when the repository is not branch-pinned"
            )
        selected_base = base_branch
    selected_base = _validate_branch(repo, selected_base, label="base_branch")
    return branch, selected_base


def _require_remote_available(repo: Path, remote: str) -> None:
    """Reject an unconfigured remote."""
    remotes = {
        line.strip()
        for line in _git(repo, ["remote"], operation="remote inventory").stdout.splitlines()
        if line.strip()
    }
    if remote not in remotes:
        raise WorktreeCreateError(
            f"configured remote '{remote}' is unavailable"
        )


def _require_branch_available(repo: Path, branch: str) -> None:
    """Reject an already-existing branch."""
    branch_ref = f"refs/heads/{branch}"
    existing = _git(
        repo,
        ["show-ref", "--verify", "--quiet", branch_ref],
        operation="branch existence check",
        check=False,
    )
    if existing.returncode == 0:
        raise WorktreeCreateError(f"branch already exists: {branch}")
    if existing.returncode not in (0, 1):
        raise WorktreeCreateError("branch existence check failed")


def _create_worktree(
    *,
    policy: RepoPolicy,
    thread_id: str,
    branch: str,
    descriptor: str,
    remote: str,
    base_branch: str | None,
) -> dict[str, Any]:
    repo = policy.path.resolve(strict=True)
    _validate_worktree_request(
        thread_id=thread_id, descriptor=descriptor, remote=remote
    )
    branch, selected_base = _resolve_branch_and_base(
        repo, policy, branch, base_branch
    )
    _require_remote_available(repo, remote)

    worktrees = repo / ".worktrees"
    if worktrees.is_symlink():
        raise WorktreeCreateError("repository .worktrees directory cannot be a symlink")
    worktree = worktrees / f"{thread_id}-{descriptor}"
    if worktree.exists() or worktree.is_symlink():
        raise WorktreeCreateError(f"worktree path already exists: {worktree}")

    _require_branch_available(repo, branch)

    _git(repo, ["fetch", remote], operation=f"fetch from {remote}")
    fetched_at = datetime.now(timezone.utc).isoformat()
    remote_ref = f"refs/remotes/{remote}/{selected_base}"
    base_sha = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{remote_ref}^{{commit}}"],
        operation=f"resolve {remote}/{selected_base}",
    ).stdout.strip()

    worktrees.mkdir(exist_ok=True)
    _git(
        repo,
        [
            "worktree",
            "add",
            "--no-track",
            "-b",
            branch,
            str(worktree),
            base_sha,
        ],
        operation="worktree creation",
    )
    created_sha = _git(
        worktree,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        operation="created worktree verification",
    ).stdout.strip()
    if created_sha != base_sha:
        raise WorktreeCreateError(
            "created worktree does not match the fetched base commit"
        )

    return {
        "base_branch": selected_base,
        "base_sha": base_sha,
        "branch": branch,
        "created": True,
        "fetched_at": fetched_at,
        "remote": remote,
        "repo_name": policy.repo_name,
        "worktree_path": str(worktree),
    }


class WorktreeCreateTool(Tool):
    """Create a new task worktree from a freshly fetched remote branch."""

    @property
    def name(self) -> str:
        """Return the stable agent-facing tool name."""
        return "worktree_create"

    @property
    def description(self) -> str:
        """Describe the atomic fresh-base worktree operation."""
        return (
            "Create a new linked Git worktree for a registered project repository. "
            "This operation fetches the configured remote first, resolves the exact "
            "remote base commit, and creates the new branch from that commit. Use "
            "this instead of raw git worktree add -b for new branches in protected "
            "base checkouts. Existing local branches may still be reattached with Git."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Return the JSON schema accepted by the tool."""
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["repo_name", "branch", "descriptor"],
            "properties": {
                "repo_name": {
                    "type": "string",
                    "description": "Registered repository name in the current project",
                },
                "branch": {
                    "type": "string",
                    "description": "New local branch name",
                },
                "descriptor": {
                    "type": "string",
                    "description": (
                        "Short worktree label; the current thread id is prepended"
                    ),
                },
                "remote": {
                    "type": "string",
                    "description": "Configured remote name (default: origin)",
                },
                "base_branch": {
                    "type": "string",
                    "description": (
                        "Remote base branch; defaults to the repository's pinned branch"
                    ),
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        """Create the worktree and return a commit-bound JSON receipt."""
        thread_id = kwargs.get("_thread_id")
        project_id = kwargs.get("_thread_project_id")
        if not isinstance(thread_id, str) or not thread_id:
            return "Error: worktree creation requires an exact current thread"
        if not isinstance(project_id, str) or not project_id:
            return "Error: worktree creation requires a current project"

        repo_name = kwargs.get("repo_name")
        branch = kwargs.get("branch")
        descriptor = kwargs.get("descriptor")
        if not isinstance(repo_name, str) or not repo_name:
            return "Error: repo_name is required"
        if not isinstance(branch, str) or not branch:
            return "Error: branch is required"
        if not isinstance(descriptor, str) or not descriptor:
            return "Error: descriptor is required"

        try:
            policy = await asyncio.to_thread(_project_policy, project_id, repo_name)
            receipt = await asyncio.to_thread(
                _create_worktree,
                policy=policy,
                thread_id=thread_id,
                branch=branch,
                descriptor=descriptor,
                remote=str(kwargs.get("remote") or "origin"),
                base_branch=(
                    str(kwargs["base_branch"])
                    if kwargs.get("base_branch") is not None
                    else None
                ),
            )
        except WorktreeCreateError as exc:
            emit_product_event(
                "worktree",
                "create_failed",
                thread_id=thread_id,
                properties={
                    "project_id": project_id,
                    "repo_name": repo_name,
                    "reason": str(exc)[:200],
                },
            )
            return f"Error: {exc}"
        emit_product_event(
            "worktree",
            "created",
            thread_id=thread_id,
            properties={
                "base_branch": receipt["base_branch"],
                "base_sha": receipt["base_sha"],
                "branch": receipt["branch"],
                "project_id": project_id,
                "remote": receipt["remote"],
                "repo_name": receipt["repo_name"],
            },
        )
        return json.dumps(receipt, sort_keys=True)
