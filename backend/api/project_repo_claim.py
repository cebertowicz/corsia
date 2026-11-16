"""What a repository registration claims: its own project, or everywhere.

A registration whose every path (the target, written and resolved, and the source
binding it creates) sits inside the registering project's own folder under
``projects/``, and overlaps no other project's folder or registered repository,
claims only that project. Every caller not reaching the project is already walled
from that folder (``tools.project_fence``), and the workspace is already writable,
so the registration makes no path newly writable for anyone outside it.

Anything else claims everywhere (``None``), the instance-wide fence: a target
outside the workspace, inside the workspace but outside every project folder,
touching another project, or any read that fails. Each such fallback is logged
with its reason, so production can tell why a registration waited for everyone.

Another project whose folder does not resolve (an archived project whose folder
is gone, say) has no folder to overlap and is skipped, as the project fence
skips it (``tools.project_fence._folder_roots``); its registered repositories
still count.

The claim is read before the transition admits; the add resolves its paths again
inside it. A narrowed add checks what it resolved with the same predicate
(:func:`confirmed_repo_path`) and refuses, to be retried, if a folder or link
changed in between and the paths now leave the project's folder.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from fastapi import HTTPException
from loguru import logger
from infra.repo_policy_transition import (
    EVERYWHERE,
    Holder,
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


def _everywhere(project_id: str, reason: str) -> frozenset[str] | None:
    """The claim ``None`` (everywhere) for ``reason``, logged where production log reads see it."""
    logger.info("repo registration claims everywhere project={} reason={}", project_id, reason)
    return None


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
    """Every other project's folder and registered repo, each as its forms; ``None`` on a bad repo row.

    A project whose folder does not resolve has no folder root, as in the project
    fence; its registered repositories are still read in the second loop.
    """
    from core.project_context import resolve_project_row_paths

    roots: list[tuple[Path, ...]] = []
    for row in db.execute("SELECT id, name, path FROM projects WHERE id != ?", (project_id,)):
        try:
            _name, root = resolve_project_row_paths(workspace, project_name=row["name"], raw_path=row["path"])
        except (ValueError, OSError):
            root = None
        if root is not None:
            roots.append(_forms(root))
    for row in db.execute(
        "SELECT project_id, repo_path FROM project_repo_policies WHERE project_id != ?", (project_id,)
    ):
        raw = row["repo_path"]
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            _everywhere(project_id, f"unreadable repo path registered by project {row['project_id']}")
            return None
        roots.append(_forms(Path(os.path.normpath(raw))))
    return roots


def _paths_claim(
    project_id: str, ws_dir: Path, paths: list[Path], *, workspace: Path, db,
) -> frozenset[str] | None:
    """``{project_id}`` when every path stays inside that project's folder and no other's.

    ``paths[0]`` is the repository folder; the rest are the source bindings.
    """
    try:
        projects_root = canonical(Path(workspace)) / "projects"
        own = canonical(ws_dir)
        # Only a direct child of ``projects/`` is a folder the project fence walls.
        if containment_key(own.parent) != containment_key(projects_root) or own == projects_root:
            return _everywhere(project_id, "project folder is not a direct child of projects/")
        own_forms = _forms(ws_dir)
        target_forms = [form for target in paths for form in _forms(target)]
        if not all(any(contains(root, form) for root in own_forms) for form in target_forms):
            return _everywhere(project_id, "a path leaves the project's folder")
        if not contains(own, canonical(paths[0])):
            return _everywhere(project_id, "the repository folder resolves outside the project's folder")
        others = _other_roots(Path(workspace), project_id, db)
        if others is None:
            return None
        if any(_overlaps(tuple(target_forms), other) for other in others):
            return _everywhere(project_id, "a path overlaps another project's folder or repository")
        return frozenset({project_id})
    except Exception:
        log.exception("repo registration claim read failed for project=%s", project_id)
        return None


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
    paths = _registration_paths(ws_dir, mode, path, name)
    if paths is None:
        return _everywhere(project_id, f"no absolute target for mode {mode}")
    return _paths_claim(project_id, ws_dir, paths, workspace=workspace, db=db)


_CHANGED = (
    "The repository's folder changed while it was being registered; check the path and try again."
)


def confirmed_repo_path(
    claim: frozenset[str] | None, project_id: str, ws_dir: Path, row: dict,
    *, checked: Path | None = None,
) -> Path:
    """The add's one resolved repository folder, which every later step of the add uses.

    Called inside the transition, once. ``checked`` is the resolved folder a caller
    vetted before the transition (the agent tool's fence and Git-config checks); the
    add registers only that folder. Under a narrowed claim, the folder and its
    ``source/`` binding must still earn that claim; an add admitted everywhere
    (``claim is None``) needs no claim check.

    Raises:
        HTTPException: 409, retryable, when the folder is not the one checked or no
            longer earns the narrowed claim.
    """
    repo_path = Path(str(row["path"])).expanduser().resolve(strict=False)
    # Exact, not case-folded: a link swapped after the caller's checks must not pass.
    if checked is not None and repo_path != checked:
        raise HTTPException(409, _CHANGED)
    if claim is None:
        return repo_path
    binding = ws_dir / "source" / str(row["name"])
    from infra import config
    from infra.database import get_db

    db = get_db()
    try:
        holds = _paths_claim(
            project_id, ws_dir, [repo_path, binding], workspace=Path(config.WORKSPACE), db=db,
        ) == claim
    finally:
        db.close()
    if not holds:
        raise HTTPException(
            409,
            "The repository's folder changed while it was being registered and is no longer "
            "inside this project's folder; try again.",
        )
    return repo_path


def owned_process_holders() -> tuple[Holder, ...]:
    """Each live owned process with the reach of the call that launched it."""
    from host_access.processes import PROCESS_OWNER

    return tuple(
        Holder(
            reach=record.reach or EVERYWHERE,
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
        return _everywhere(project_id, "project folder not found")
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
) -> AbstractContextManager[frozenset[str] | None]:
    """The repo policy transition one registration runs under, claiming what it changes.

    It yields the claim it was admitted under; pass that to :func:`confirmed_repo_path`.
    """
    generation = transition_generation()
    return repo_policy_transition(
        active_process_count=active_process_count,
        owned_processes=owned_process_holders,
        claim=_add_claim(project_id, mode, path, name, ws_dir_for),
        claim_generation=generation,
    )
