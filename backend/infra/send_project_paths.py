"""Project working-directory validation for provider sends."""

from __future__ import annotations

from infra.send_types import SendRejected


def _rejection(project_id: str, harness: str) -> SendRejected:
    return SendRejected(
        422,
        f"{harness} turn blocked: Project working directory unresolved "
        f"for project_id={project_id}",
        metadata={
            "event_type": "harness.error",
            "harness": harness.lower().replace(" ", "-"),
            "cause": "project_working_directory_unresolved",
            "repair_owner": "ctx-stability",
        },
    )


def validate_project_working_dir(project_id: str | None, *, harness: str) -> None:
    """Reject sends whose project cannot resolve one valid planning root."""
    if not project_id:
        return
    from core.project_context import resolve_project_paths
    from infra.config import WORKSPACE
    from infra.database import get_db

    db = get_db()
    try:
        _project_name, workspace_dir = resolve_project_paths(
            WORKSPACE,
            project_id,
            db=db,
        )
    except ValueError as exc:
        raise _rejection(project_id, harness) from exc
    finally:
        db.close()
    if workspace_dir:
        return
    raise _rejection(project_id, harness)
