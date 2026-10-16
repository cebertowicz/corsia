"""Place an imported folder-scoped app in the CORSIA project that owns its folder (A-R15)."""
from __future__ import annotations

from pathlib import Path

from loguru import logger


def _project_folders(db) -> list[tuple[str, Path]]:
    """Each project's planning root and registered repository paths."""
    from core.project_context import resolve_project_row_paths
    from infra.config import WORKSPACE
    from infra.project_repo_policy import get_project_repo_policies

    folders: list[tuple[str, Path]] = []
    for row in db.execute("SELECT id,name,path FROM projects WHERE path IS NOT NULL"):
        project_id = row["id"]
        try:
            _name, planning_root = resolve_project_row_paths(
                WORKSPACE, project_name=row["name"], raw_path=row["path"],
            )
        except ValueError:
            logger.warning("import skipped an unresolved project root for project_id={}", project_id)
            planning_root = None
        if planning_root is not None:
            folders.append((project_id, planning_root))
        folders.extend((project_id, policy.path) for policy in get_project_repo_policies(project_id, db=db).values())
    return folders


def project_reach(folder: Path | None, *, db) -> tuple[dict[str, object] | None, str | None]:
    """Return the app's reach, or None plus the folder name when no project owns the folder."""
    if folder is None:
        return {"mode": "all", "project_ids": [], "outside_projects": False}, None
    candidate = folder.expanduser().resolve(strict=False)
    matches: list[tuple[int, str]] = []
    for project_id, project_path in _project_folders(db):
        project_path = project_path.resolve(strict=False)
        if candidate == project_path or project_path in candidate.parents:
            matches.append((len(project_path.parts), project_id))
    if not matches:
        return None, folder.name or candidate.name
    _depth, project_id = max(matches)
    return {"mode": "selected", "project_ids": [project_id], "outside_projects": False}, None
