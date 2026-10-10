"""Agent-facing tool to register a repository for the current project.

Closes the discovery loop issue #1094 names: ``project_repos`` shows what is
registered, ``project_repo_register`` attaches an existing checkout so
``worktree_create`` can target it, without routing the user through Project
settings for every repository.

The procedure itself is not reimplemented here. This is a thin, deliberately
narrower front door onto ``api.project_repos.register_source_repo`` — the same
compensated flow Project settings uses — with two agent-specific restrictions:

* the target must already be a Git repository, so the tool cannot pull an
  arbitrary ``$HOME`` directory into the workspace where sandboxed file tools
  would then read it; and
* pin/protection are not agent-settable, so registering can never switch a
  branch in the user's real checkout. The policy defaults (pin to the current
  branch, protect the base checkout) come from the shared procedure, and
  changing them stays a Project-settings decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.base import Tool


class ProjectRepoRegisterTool(Tool):
    """Register an existing Git repository for the current project."""

    @property
    def name(self) -> str:
        return "project_repo_register"

    @property
    def description(self) -> str:
        return (
            "Register an existing Git repo directory for the current project "
            "so worktree_create can target it. Pins to its current branch and "
            "protects the base checkout."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Existing Git repo directory path",
                },
                "repo_name": {
                    "type": "string",
                    "description": "Optional name; default basename",
                },
            },
            "required": ["repo_path"],
        }

    async def execute(self, **kwargs: Any) -> str:
        from fastapi import HTTPException

        from api.project_repos import RepoAdd, register_source_repo
        from core.registered_repos import format_registered_repo_policy
        from infra.db_async import run_in_db
        from infra.product_events import emit_product_event
        from infra.project_repo_policy import RepoPolicy

        thread_id = kwargs.get("_thread_id")
        project_id = kwargs.get("_thread_project_id")
        if not isinstance(thread_id, str) or not thread_id:
            return "Error: project_repo_register requires an exact current thread"
        if not isinstance(project_id, str) or not project_id:
            return "Error: project_repo_register requires a current project"

        raw_path = kwargs.get("repo_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return "Error: repo_path is required"
        name = kwargs.get("repo_name")
        if name is not None and not (isinstance(name, str) and name.strip()):
            return "Error: repo_name must be a non-empty string when provided"

        try:
            target = Path(raw_path).expanduser().resolve(strict=True)
        except OSError as exc:
            return f"Error: repo_path could not be resolved: {exc}"
        if not target.is_dir():
            return f"Error: not a directory: {target}"
        # The agent surface attaches repositories, never arbitrary folders:
        # a non-repo directory linked into source/ would become readable
        # through the workspace-scoped file tools.
        if not (target / ".git").exists():
            return (
                f"Error: not a Git repository: {target}. "
                "project_repo_register only attaches Git repositories; use "
                "Project settings to add a non-repository folder."
            )

        body = RepoAdd(mode="link", path=str(target), name=name)
        try:
            repo = await run_in_db(
                lambda: register_source_repo(project_id, body)
            )
        except HTTPException as exc:
            return f"Error: {exc.detail}"
        except (ValueError, OSError, RuntimeError) as exc:
            return f"Error: {exc}"

        policy = RepoPolicy(
            project_id=project_id,
            repo_name=str(repo["name"]),
            path=Path(str(repo["path"])),
            pinned_branch=repo.get("pinned_branch"),
            protect_base_checkout=bool(repo.get("protect_base_checkout")),
        )
        emit_product_event(
            "project_repo",
            "registered",
            thread_id=thread_id,
            properties={
                "project_id": project_id,
                "repo_name": policy.repo_name,
                "repo_path": str(policy.path),
                "pinned_branch": policy.pinned_branch,
                "protect_base_checkout": policy.protect_base_checkout,
            },
        )
        return (
            f"Registered repository '{policy.repo_name}'\n"
            f"- {format_registered_repo_policy(policy.repo_name, policy)}"
        )


__all__ = ["ProjectRepoRegisterTool"]
