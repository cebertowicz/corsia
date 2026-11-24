"""Person ownership for project and agent priorities documents."""
from __future__ import annotations

from pathlib import Path


def is_priorities_path(path: Path, *, workspace: Path, db) -> bool:
    """Identify only priorities files at the two instruction-file locations."""
    from registry.shared_principals import document_project

    root, project_id = document_project(path, workspace=workspace, db=db)
    try:
        parts = path.resolve(strict=False).relative_to(root).parts
    except ValueError:
        return False
    return ((project_id is not None and parts == ("context", "PRIORITIES.md"))
            or (len(parts) == 4 and parts[0] == ".threads" and bool(parts[1])
                and parts[2:] == ("context", "PRIORITIES.md")))


def register_new_person_owner(identity, existing, metadata, title, description, retention, *, db) -> None:
    """Give a newly registered priorities file its person owner in the registration transaction."""
    if existing is not None or identity.zone != "workspace" or Path(identity.canonical).name != "PRIORITIES.md":
        return
    from infra import config
    from registry import models
    from registry.shared_principals import UI_PRINCIPAL

    if is_priorities_path(Path(identity.canonical), workspace=Path(config.WORKSPACE), db=db):
        models.register_artifact(
            integration="workspace", type=identity.artifact_type, external_id=identity.external_id,
            parent_thread_id=UI_PRINCIPAL, title=title or Path(identity.canonical).name,
            description=description, path=identity.canonical, retention=retention,
            retention_default=identity.ephemeral_default, metadata=metadata, db=db,
        )


def migrate_priorities_ownership(workspace: Path, *, db) -> int:
    """Register and hand existing priorities files to the person once."""
    from core.workspace_layout import resolve_project_root
    from infra.settings import get_setting, set_setting
    from registry import service
    from registry.ownership import rehome_artifact
    from registry.shared_principals import UI_PRINCIPAL, artifact_owner

    marker = "priorities_person_ownership_v1"
    if get_setting(marker, db=db):
        return 0
    roots = [(workspace.resolve(), False)]
    for row in db.execute("SELECT id FROM projects").fetchall():
        root = resolve_project_root(workspace, row["id"], db=db)
        if root is not None:
            roots.append((root.resolve(), True))
    count = 0
    for root, project in roots:
        paths = list((root / ".threads").glob("*/context/PRIORITIES.md"))
        if project:
            paths.append(root / "context" / "PRIORITIES.md")
        for path in paths:
            if not path.is_file() or path.is_symlink() or not is_priorities_path(path, workspace=workspace, db=db):
                continue
            artifact = service.find_file_identity(str(path), db=db)
            if artifact is None:
                artifact = service.register_write_strict(UI_PRINCIPAL, str(path), db=db)
            if artifact_owner(str(artifact["id"]), db=db) != UI_PRINCIPAL:
                rehome_artifact(integration=artifact["integration"], type=artifact["type"],
                                external_id=artifact["external_id"], new_owner_thread_id=UI_PRINCIPAL,
                                actor_thread_id=None, authority="migration", reason="priorities belong to the person",
                                source_path="registry.priorities_ownership", db=db, show_to_new_owner=False)
            count += 1
    set_setting(marker, {"done": True}, db=db)
    return count
