"""What a repository registration claims: its own project, or everywhere.

A registration whose every path (the target, written and resolved, and the source
binding it creates) sits inside the registering project's own folder under
``projects/``, and overlaps no other project's folder or registered repository,
claims only that project. Every caller not reaching the project is already walled
from that folder (``tools.project_fence``), and the workspace is already writable,
so the registration makes no path newly writable for anyone outside it.

Anything else claims everywhere (``None``), the instance-wide fence: a target
outside the workspace, inside the workspace but outside every project folder,
touching another project, or any read that fails.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from fastapi import HTTPException
from infra.repo_policy_transition import (
    EVERYWHERE,
    Holder,
    Reach,
    repo_policy_transition,
    transition_generation,
)
from tools.host_path_resolution import canonical, containment_key, contains

log = logging.getLogger(__name__)


def _forms(path: Path) -> tuple[Path, ...]:
    """The path as written (absolute, normalized) and resolved."""
    return tuple(dict.fromkeys((Path(os.path.abspath(path)), canonical(path))))


def _overlaps(a: tuple[Path, ...], b: tuple[Path, ...]) -> bool:
    return any(contains(x, y) or contains(y, x) for x in a for y in b)


def _registration_paths(ws_dir: Path, mode: str, path: str | None, name: str | None) -> list[Path] | None:
    source = ws_dir / "source"
    if mode == "link":
        if not path:
            return None
        target = Path(path).expanduser()
        if not target.is_absolute():
            return None
        return [target, source / name if name else source]
    if mode == "create":
        return [source / name if name else source]
    return None


def _other_roots(workspace: Path, project_id: str, db) -> list[tuple[Path, ...]] | None:
    """Every other project's folder and registered repo, each as its forms; ``None`` on a bad row."""
    from core.project_context import resolve_project_row_paths

    roots: list[tuple[Path, ...]] = []
    for row in db.execute("SELECT id, name, path FROM projects WHERE id != ?", (project_id,)):
        _name, root = resolve_project_row_paths(workspace, project_name=row["name"], raw_path=row["path"])
        if root is None:
            return None
        roots.append(_forms(root))
    for row in db.execute(
        "SELECT repo_path FROM project_repo_policies WHERE project_id != ?", (project_id,)
    ):
        raw = row["repo_path"]
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            return None
        roots.append(_forms(Path(os.path.normpath(raw))))
    return roots


def registration_claim(
    project_id: str,
    ws_dir: Path,
    *,
    mode: str,
    path: str | None,
    name: str | None,
    workspace: Path,
    db,
) -> frozenset[str] | None:
    """``{project_id}`` when the registration stays inside that project's folder, else ``None``."""
    try:
        paths = _registration_paths(ws_dir, mode, path, name)
        if paths is None:
            return None
        projects_root = canonical(Path(workspace)) / "projects"
        own = canonical(ws_dir)
        # Only a direct child of ``projects/`` is a folder the project fence walls.
        if containment_key(own.parent) != containment_key(projects_root) or own == projects_root:
            return None
        own_forms = _forms(ws_dir)
        target_forms = [form for target in paths for form in _forms(target)]
        if not all(any(contains(root, form) for root in own_forms) for form in target_forms):
            return None
        if not contains(own, canonical(paths[0])):
            return None
        others = _other_roots(Path(workspace), project_id, db)
        if others is None or any(_overlaps(tuple(target_forms), other) for other in others):
            return None
        return frozenset({project_id})
    except Exception:
        log.exception("repo registration claim read failed for project=%s", project_id)
        return None


def owned_process_holders() -> tuple[Holder, ...]:
    """Each live owned process with the reach of the call that launched it."""
    from host_access.processes import PROCESS_OWNER

    return tuple(
        Holder(
            reach=record.reach if isinstance(record.reach, Reach) else EVERYWHERE,
            thread_id=record.thread_id,
            what=(
                f'process "{record.description}"' if record.description
                else f"a {record.mode} process"
            ),
        )
        for record in PROCESS_OWNER.outstanding_records()
    )


def _add_claim(
    project_id: str, mode: str, path: str | None, name: str | None, ws_dir_for: Callable[[str], Path],
) -> frozenset[str] | None:
    from infra import config
    from infra.database import get_db

    try:
        ws_dir = ws_dir_for(project_id)
    except HTTPException:
        # The add itself reports the missing project once the transition admits it.
        return None
    db = get_db()
    try:
        return registration_claim(
            project_id, ws_dir, mode=mode, path=path, name=name,
            workspace=Path(config.WORKSPACE), db=db,
        )
    finally:
        db.close()


def add_transition(
    project_id: str,
    mode: str,
    path: str | None,
    name: str | None,
    *,
    ws_dir_for: Callable[[str], Path],
    active_process_count: Callable[[], int],
) -> AbstractContextManager[None]:
    """The repo policy transition one registration runs under, claiming what it changes."""
    generation = transition_generation()
    return repo_policy_transition(
        active_process_count=active_process_count,
        owned_processes=owned_process_holders,
        claim=_add_claim(project_id, mode, path, name, ws_dir_for),
        claim_generation=generation,
    )
