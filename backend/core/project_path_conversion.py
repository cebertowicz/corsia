"""Startup conversion of legacy absolute project paths to logical references.

This runs on every start, not in the schema migration ladder, because each
row's outcome depends on the filesystem at that moment: whether the stored
checkout exists and can be linked into ``source/``. A project with no
project folder gets the standard one. A row is rewritten only when nothing
is lost; any other row keeps its stored path verbatim, still resolves to its
project folder, and is retried on the next start, so a checkout on a volume
that is unmounted today converts once it is back. Only already-logical rows
are skipped, and current writers store logical references, so the sweep is a
single SELECT once every legacy row has converted. Remove it once every
supported install has started a release that contains it (debt ledger:
legacy project-path converter).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from api.project_scaffolding import (
    link_repo_into_source,
    remove_owned_source_entry,
    validate_repo_name,
)
from core.project_context import legacy_planning_root, project_slug
from core.workspace_scaffolding import scaffold_project
from infra.database import write_transaction
from infra.platform_paths import WorkspaceSelection
from infra.project_repo_policy import register_repo_policy
from infra.workspace_paths import WorkspaceReference, looks_like_workspace_reference


@dataclass(frozen=True, slots=True)
class ProjectPathConversionResult:
    """Counts from one idempotent legacy project-path conversion pass."""

    converted: int
    kept_legacy: int


def _row_value(row: Any, key: str) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key)
    return None


def _scaffold_planning_root(
    selected_root: Path,
    project_name: object,
    description: object,
) -> Path | None:
    """Create the standard project folder for a legacy row that has none."""
    slug = project_slug(project_name)
    if slug is None:
        return None
    try:
        target = scaffold_project(
            selected_root / "projects" / slug,
            name=str(project_name).strip(),
            description=description if isinstance(description, str) else "",
        )
    except (OSError, ValueError):
        return None
    return target.resolve(strict=False)


def _legacy_checkout(raw_path: object) -> Path | None:
    """Return an existing absolute legacy checkout path, if one was stored."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _bound_source_name(planning_root: Path, checkout: Path) -> str | None:
    """Return an existing source entry bound to ``checkout``, if any."""
    source_dir = planning_root / "source"
    if not source_dir.is_dir():
        return None
    for entry in sorted(source_dir.iterdir(), key=lambda path: path.name):
        if entry.name.startswith(".") or not (entry.exists() or entry.is_symlink()):
            continue
        if entry.resolve(strict=False) == checkout:
            return entry.name
    return None


def _policy_names(project_id: str, connection) -> set[str]:
    """Return every persisted policy name, including malformed legacy rows."""
    rows = connection.execute(
        "SELECT repo_name FROM project_repo_policies WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    return {str(_row_value(row, "repo_name")) for row in rows}


def _available_source_name(
    planning_root: Path,
    checkout: Path,
    policy_names: set[str],
) -> str:
    """Return a deterministic unused source name for one checkout."""
    base = checkout.name
    if validate_repo_name(base):
        raise ValueError(f"invalid source name derived from checkout: {base!r}")
    source_dir = planning_root / "source"
    suffix = 1
    while True:
        name = base if suffix == 1 else f"{base}-{suffix}"
        if (
            name not in policy_names
            and not (source_dir / name).exists()
            and not (source_dir / name).is_symlink()
        ):
            return name
        suffix += 1


def _names_planning_location(raw_path: object) -> bool:
    """Return whether a stored path is a project folder, not a checkout.

    The retired resolver read a stored ``.../projects/<name>`` path as the
    planning root whenever a selected project folder matched it, and every
    other stored path as a repo checkout.
    """
    return not raw_path or Path(str(raw_path)).expanduser().parent.name == "projects"


def _preserve_checkout(
    project_id: str,
    planning_root: Path,
    raw_path: object,
    connection,
    *,
    folder_existed: bool,
) -> str | None:
    """Record a stored checkout as a repo policy; return why the row must be kept.

    A stored project-folder path is superseded by the selected folder, as the
    retired resolver read it; one with no selected match is preserved like a
    checkout when it still exists (it may be the only copy of that folder).
    """
    checkout = _legacy_checkout(raw_path)
    if _names_planning_location(raw_path) and (folder_existed or checkout is None):
        return None
    if checkout is None:
        return "stored checkout is not reachable; retried on the next start"
    if checkout == planning_root:
        return None

    policy_names = _policy_names(project_id, connection)
    name = _bound_source_name(planning_root, checkout)
    identity = None
    if name is None:
        try:
            name = _available_source_name(planning_root, checkout, policy_names)
            _row, identity = link_repo_into_source(
                planning_root, checkout, name, with_ownership=True
            )
        except ValueError:
            return "checkout could not be linked into source"
    if name in policy_names:
        return None
    try:
        register_repo_policy(
            project_id,
            name,
            checkout,
            pinned_branch=None,
            protect_base_checkout=False,
            db=connection,
        )
    except sqlite3.Error:
        if identity is not None:
            remove_owned_source_entry(identity, missing_ok=True)
        raise
    return None


def _log_kept(project_id: str, raw_path: object, reason: str) -> None:
    logger.warning(
        "legacy project path conversion kept project_id={} prior_path={!r}: {}",
        project_id,
        raw_path,
        reason,
    )


def convert_legacy_project_paths(
    selection: WorkspaceSelection,
    *,
    db=None,
) -> ProjectPathConversionResult:
    """Rewrite legacy project paths whose stored value is fully preserved."""
    converted = 0
    kept_legacy = 0
    selected_root = selection.root.resolve(strict=False)
    with write_transaction(db, scope="convert_legacy_project_paths") as connection:
        rows = connection.execute(
            "SELECT id, name, path, description FROM projects ORDER BY id"
        ).fetchall()
        for row in rows:
            raw_path = _row_value(row, "path")
            if looks_like_workspace_reference(raw_path):
                continue
            project_id = str(_row_value(row, "id"))
            name = _row_value(row, "name")
            planning_root = legacy_planning_root(selected_root, name, raw_path)
            folder_existed = planning_root is not None
            if planning_root is None:
                if not raw_path:
                    continue
                planning_root = _scaffold_planning_root(
                    selected_root, name, _row_value(row, "description")
                )
                if planning_root is None:
                    _log_kept(project_id, raw_path, "no project folder could be created")
                    kept_legacy += 1
                    continue
                logger.info(
                    "legacy project path conversion created project folder "
                    "project_id={} folder={}",
                    project_id,
                    planning_root,
                )
            reason = _preserve_checkout(
                project_id,
                planning_root,
                raw_path,
                connection,
                folder_existed=folder_existed,
            )
            if reason is not None:
                _log_kept(project_id, raw_path, reason)
                kept_legacy += 1
                continue
            reference = WorkspaceReference.from_path(planning_root, selection).serialize()
            connection.execute(
                "UPDATE projects SET path = ? WHERE id = ?",
                (reference, project_id),
            )
            converted += 1

    result = ProjectPathConversionResult(converted=converted, kept_legacy=kept_legacy)
    if result.converted or result.kept_legacy:
        logger.info(
            "legacy project path conversion: converted={} kept_legacy={}",
            result.converted,
            result.kept_legacy,
        )
    return result
