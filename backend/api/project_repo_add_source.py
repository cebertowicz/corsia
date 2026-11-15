"""The source binding a repository registration uses, resolved before its transaction.

A link-mode target outside ``source/`` is linked in, and create mode makes a new repo.
A top-level entry of ``<project>/source/`` (a folder, or a link to one) that has no
registration is registered where it is, under its own name: no link is created and
nothing moves. A path nested inside an entry, an entry that is already registered,
and a link that points nowhere are refused with what to do instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from api.project_scaffolding import (
    SourceEntryIdentity,
    _repo_row,
    capture_source_entry,
    create_repo_in_source,
    link_repo_into_source,
)


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def source_entry_for(ws_dir: Path, target: Path) -> Path | None:
    """The top-level ``source/`` entry ``target`` names, or ``None`` when it is outside ``source/``.

    Raises:
        HTTPException: 400 when ``target`` is ``source/`` itself or nested inside an entry.
    """
    source_dir = ws_dir / "source"
    source_unresolved = Path(os.path.normpath(source_dir.absolute()))
    source_resolved = source_dir.resolve()
    unresolved = Path(os.path.normpath(target.expanduser().absolute()))
    resolved = target.expanduser().resolve()
    for candidate, root in ((unresolved, source_unresolved), (resolved, source_resolved)):
        if not _within(candidate, root):
            continue
        if candidate == root:
            raise HTTPException(400, "Register one repository inside source/, not source/ itself")
        top = root / candidate.relative_to(root).parts[0]
        if candidate != top:
            raise HTTPException(
                400,
                f"'{target}' is inside the source entry '{top.name}'; register '{top}' instead",
            )
        return source_dir / top.name
    return None


def capture_in_place_entry(
    ws_dir: Path, entry: Path, *, name: str | None, registered: set[str],
) -> tuple[dict, SourceEntryIdentity]:
    """The row and identity of an unregistered ``source/`` entry, registered where it is."""
    if name is not None and name != entry.name:
        raise HTTPException(
            400,
            f"'{entry.name}' is already in source/ and keeps its name; leave out the repository name",
        )
    if entry.name.startswith("."):
        raise HTTPException(400, f"'{entry.name}' is a hidden entry in source/, not a repository")
    if entry.name in registered:
        raise HTTPException(409, f"Repository '{entry.name}' is already registered for this project")
    if entry.is_symlink():
        try:
            target = entry.resolve(strict=True)
        except OSError as exc:
            raise HTTPException(400, f"source/{entry.name} is a link that points nowhere") from exc
        if not target.is_dir():
            raise HTTPException(400, f"source/{entry.name} is a link that does not point to a folder")
        kind = "linked"
    elif entry.is_dir():
        target, kind = entry.resolve(), "native"
    else:
        raise HTTPException(400, f"source/{entry.name} is not a folder")
    return _repo_row(entry.name, target, kind, False), capture_source_entry(entry, kind)


def _registered_names(project_id: str) -> set[str]:
    from infra.database import get_db

    db = get_db()
    try:
        return {
            str(row["repo_name"]) for row in db.execute(
                "SELECT repo_name FROM project_repo_policies WHERE project_id = ?",
                (project_id,),
            )
        }
    finally:
        db.close()


def resolve_add_source(
    project_id: str, ws_dir: Path, *, mode: str, path: str | None, name: str | None,
) -> tuple[dict, SourceEntryIdentity, bool]:
    """The row and identity an add registers, and whether the add created that binding.

    Only a created binding is removed when the add fails; an entry already in ``source/``
    is registered where it is and left alone.
    """
    if mode == "link":
        if not path:
            raise HTTPException(400, "link mode requires 'path'")
        try:
            entry = source_entry_for(ws_dir, Path(path))
            if entry is None:
                row, identity = link_repo_into_source(ws_dir, Path(path), name=name, with_ownership=True)
                return row, identity, True
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"Bad repository path: {exc}") from exc
        registered = _registered_names(project_id)
        return *capture_in_place_entry(ws_dir, entry, name=name, registered=registered), False
    if not name:
        raise HTTPException(400, "create mode requires 'name'")
    try:
        row, identity = create_repo_in_source(ws_dir, name, with_ownership=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return row, identity, True
