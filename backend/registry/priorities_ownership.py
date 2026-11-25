"""Person ownership for project and agent priorities documents."""
from __future__ import annotations

from pathlib import Path


def _at_priorities_location(path: Path, root: Path, *, project: bool) -> bool:
    """True when the resolved path sits at a priorities location under the resolved root."""
    try:
        parts = path.resolve(strict=False).relative_to(root).parts
    except ValueError:
        return False
    return ((project and parts == ("context", "PRIORITIES.md"))
            or (len(parts) == 4 and parts[0] == ".threads" and bool(parts[1])
                and parts[2:] == ("context", "PRIORITIES.md")))


def is_priorities_path(path: Path, *, workspace: Path, db) -> bool:
    """Identify only priorities files at the two instruction-file locations."""
    from registry.shared_principals import document_project

    root, project_id = document_project(path, workspace=workspace, db=db)
    return _at_priorities_location(path, root, project=project_id is not None)


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
    """Give unregistered priorities files to the person at every start; hand registered ones over once.

    Registered files are rehomed only on the first run, so a file the person later gives to an
    agent stays with that agent.
    """
    from core.workspace_layout import resolve_project_root
    from infra.settings import get_setting, set_setting
    from registry import service
    from registry.ownership import rehome_artifact
    from registry.shared_principals import UI_PRINCIPAL, artifact_owner

    marker = "priorities_person_ownership_v1"
    handed_over = bool(get_setting(marker, db=db))
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
            if (not path.is_file() or path.is_symlink()
                    or not _at_priorities_location(path, root, project=project)):
                continue
            artifact = service.find_file_identity(str(path), db=db)
            if artifact is None:
                service.register_write_strict(UI_PRINCIPAL, str(path), db=db)
                count += 1
            elif not handed_over and artifact_owner(str(artifact["id"]), db=db) != UI_PRINCIPAL:
                rehome_artifact(integration=artifact["integration"], type=artifact["type"],
                                external_id=artifact["external_id"], new_owner_thread_id=UI_PRINCIPAL,
                                actor_thread_id=None, authority="migration", reason="priorities belong to the person",
                                source_path="registry.priorities_ownership", db=db, show_to_new_owner=False)
                count += 1
    if not handed_over:
        set_setting(marker, {"done": True}, db=db)
    return count
