"""Seed projects for Connected Apps import tests in the shape production stores."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def seed_project(db: sqlite3.Connection, project_id: str, folder: Path) -> None:
    """Store a logical planning root, as project creation does, plus one linked repository folder."""
    from infra import config
    from infra.workspace_paths import WorkspaceReference

    reference = WorkspaceReference.from_path(
        Path(config.WORKSPACE) / "projects" / project_id, config.WORKSPACE_SELECTION,
    ).serialize()
    db.execute(
        "INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        (project_id, project_id, reference, "t", "t"),
    )
    db.execute(
        "INSERT INTO project_repo_policies VALUES (?, 'repo', ?, NULL, 1)",
        (project_id, str(folder)),
    )
    db.commit()
