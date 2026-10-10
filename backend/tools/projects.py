"""Project management tools — create projects with workspace scaffolding."""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool


def _caller_owner_and_model(db, caller_thread_id: str | None) -> tuple[str, str] | str:
    """Return the caller's (user_id, conductor model), or a refusal message."""
    from llm.delegation_model import eligible_delegation_model

    if not caller_thread_id:
        return (
            "❌ Project creation requires the calling thread's owner "
            "as the conductor; no calling thread was provided"
        )
    caller = db.execute(
        "SELECT user_id, model FROM threads WHERE id = ?",
        (caller_thread_id,),
    ).fetchone()
    if caller is None:
        return f"❌ Calling thread not found: {caller_thread_id}"
    # The conductor inherits the caller's model so its first input runs; a
    # model that is not ready would hold it instead.
    conductor_model, refusal = eligible_delegation_model(caller["model"])
    if conductor_model is None:
        return (
            "❌ The project's conductor runs on your current model, "
            f"which cannot bind: {refusal} Switch to a ready model, "
            "then create the project."
        )
    return caller["user_id"], conductor_model


class CreateProjectTool(Tool):
    """Create a new project with workspace scaffolding."""

    @property
    def name(self) -> str:
        return "project_create"

    @property
    def description(self) -> str:
        return (
            "Create a new project with workspace directory structure. "
            "Auto-creates canonical context/, skills/, shared/, .threads/, and "
            "source/ directories, seeds context/PROJECT.md, and registers the "
            "project's document roots.\n\n"
            "USE THIS when:\n"
            "- The user wants to start a new project\n"
            "- An idea has been validated and is ready to become a project\n"
            "- You need to create a project programmatically\n\n"
            "The project's conductor runs on your current model, which must "
            "read ready; otherwise this refuses and names ready ones. Returns "
            "the project ID, workspace path, conductor_thread_id and "
            "conductor_model."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name (must be unique)",
                },
                "description": {
                    "type": "string",
                    "description": "Project description — what it is and why it exists",
                },
                "status": {
                    "type": "string",
                    "enum": ["idea", "exploring", "validated", "active", "archived"],
                    "description": "Initial project status (default: active)",
                },
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Optional: path to an existing repo to attach to this project. "
                        "It is LINKED as workspace/projects/{slug}/source/<basename> "
                        "(a symlink — the repo itself is never moved or modified); "
                        "more repos can be added later in Project settings."
                    ),
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        from infra.database import get_db
        from api.projects import VALID_PROJECT_STATUSES
        from api.project_events import notify_projects_changed
        from api.project_scaffolding import initialize_project
        from core.project_context import resolve_project_paths
        from infra import config

        caller_thread_id = kwargs.pop("_thread_id", None)

        name = kwargs["name"]
        description = kwargs.get("description", "")
        status = kwargs.get("status", "active")
        repo_path_str = kwargs.get("repo_path")

        if status not in VALID_PROJECT_STATUSES:
            return f"❌ Invalid status '{status}'. Must be one of: {sorted(VALID_PROJECT_STATUSES)}"

        def _run():
            db = get_db()
            try:
                existing = db.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
                if existing:
                    return f"❌ Project '{name}' already exists (id: {existing['id']})"
                resolved = _caller_owner_and_model(db, caller_thread_id)
                if isinstance(resolved, str):
                    return resolved
                caller_user_id, conductor_model = resolved

                # Single under-the-hood init shared with POST /api/projects:
                # row + scaffold + bootloader files + registered doc-roots.
                project = initialize_project(
                    db,
                    name=name,
                    description=description,
                    status=status,
                    repo_path_str=repo_path_str,
                    user_id=caller_user_id,
                    conductor_model=conductor_model,
                )
                notify_projects_changed(caller_user_id)
                _project_name, workspace_path = resolve_project_paths(
                    config.WORKSPACE,
                    str(project["id"]),
                    db=db,
                )
                if workspace_path is None:
                    raise RuntimeError("created project workspace path is unresolved")

                return json.dumps({
                    "created": True,
                    "project_id": project["id"],
                    "name": project["name"],
                    "status": project["status"],
                    "workspace_path": str(workspace_path),
                    "conductor_thread_id": project["conductor_thread_id"],
                    "conductor_model": conductor_model,
                })

            except Exception as e:
                return f"❌ Failed to create project: {e}"
            finally:
                db.close()

        return await run_in_db(_run)
