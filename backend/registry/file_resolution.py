"""Canonical preparation and resolution for registered workspace files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.database import get_db
from infra.project_repo_policy import RepoPolicyQueryError, list_registered_repo_policies
from registry import models
from registry.file_identity import FileIdentity, external_identity_for_path
from registry.file_types import extension_type
from registry.identity_bridge import resolve_normalized_owners
from tools.sandbox_config import get_allowed_dirs_rw


def classify(path: Path) -> str:
    """Artifact type for a file."""
    from core.skill_validation import is_skill_file

    return "skill" if is_skill_file(path) else extension_type(path)


def is_tmp(path: Path) -> bool:
    """True if the file lives under a ``working/`` tray."""
    return "working" in {part.lower() for part in path.parts}


def registrable_zone(
    path: Path, *, db=None, allowed_roots: list[Path] | None = None,
) -> str | None:
    """Return ``workspace`` or ``repo`` when ``path`` is registrable."""
    from infra import config

    workspace = config.WORKSPACE.resolve()
    candidates = [(workspace, "workspace")]
    candidates.extend(
        (root, "repo")
        for root in {
            allowed.resolve()
            for allowed in (
                allowed_roots
                if allowed_roots is not None
                else get_allowed_dirs_rw(db=db, containing=path)
            )
            if allowed.resolve() != workspace
        }
    )
    matches = [
        (root, zone)
        for root, zone in candidates
        if path == root or path.is_relative_to(root)
    ]
    return max(matches, key=lambda match: len(match[0].parts))[1] if matches else None


def normalize_file_path(
    raw_path: str, *, db=None, allowed_roots: list[Path] | None = None,
) -> tuple[str, str, bool] | None:
    """Resolve a caller-supplied file path for registry preparation."""
    from infra import config

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config.WORKSPACE / path
    path = path.resolve()
    zone = registrable_zone(path, db=db, allowed_roots=allowed_roots)
    if zone is None:
        return None
    return str(path), zone, (is_tmp(path) if zone == "workspace" else False)


def prepare_file_identity(
    abs_path: str | FileIdentity, *, db=None, allowed_roots: list[Path] | None = None,
) -> FileIdentity | None:
    """Prepare filesystem facts once before a registry read or write."""
    if isinstance(abs_path, FileIdentity):
        return abs_path
    normalized = normalize_file_path(abs_path, db=db, allowed_roots=allowed_roots)
    if normalized is None:
        return None
    canonical, zone, ephemeral = normalized
    return FileIdentity(
        canonical,
        zone,
        ephemeral,
        classify(Path(canonical)),
        external_identity_for_path(canonical, zone, db=db),
    )


def find_file_identity(abs_path: str | FileIdentity, *, db=None) -> dict[str, Any] | None:
    """Find any identity for a file, including its legacy physical-path identity."""
    identity = prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    connection = db or get_db()
    try:
        return models.find_identity_by_external_ids(
            "workspace",
            identity.artifact_type,
            (identity.external_id, identity.canonical),
            db=connection,
        )
    finally:
        if db is None:
            connection.close()


def resolve_file_identities(abs_paths, *, db=None) -> dict[str, dict[str, Any]]:
    """Resolve active file owners with each path's containing-scoped roots."""
    paths = list(dict.fromkeys(abs_paths))
    if not paths:
        return {}
    roots_by_directory: dict[Path, list[Path]] = {}
    try:
        policies = list_registered_repo_policies(db=db)
    except RepoPolicyQueryError:
        policies = None

    def roots_for(path: str | FileIdentity) -> list[Path]:
        directory = Path(path.canonical if isinstance(path, FileIdentity) else path).parent
        if directory not in roots_by_directory:
            kwargs = {"db": db, "containing": directory}
            if policies is not None:
                kwargs["policies"] = policies
            roots_by_directory[directory] = get_allowed_dirs_rw(**kwargs)
        return roots_by_directory[directory]

    identities = {
        path: prepare_file_identity(path, db=db, allowed_roots=roots_for(path))
        for path in paths
    }
    candidates = [
        (identity.artifact_type, external_id)
        for identity in identities.values()
        if identity is not None
        for external_id in dict.fromkeys((identity.external_id, identity.canonical))
    ]
    owners = resolve_normalized_owners(db, "workspace", candidates)
    found: dict[str, dict[str, Any]] = {}
    for path, identity in identities.items():
        if identity is None:
            continue
        for external_id in dict.fromkeys((identity.external_id, identity.canonical)):
            artifact = owners.get((identity.artifact_type, external_id))
            if artifact is not None:
                found[path] = artifact
                break
    return found


def resolve_file_identity(abs_path: str | FileIdentity, *, db=None) -> dict[str, Any] | None:
    """Resolve one active file owner through the canonical batch resolver."""
    connection = db or get_db()
    try:
        return resolve_file_identities([abs_path], db=connection).get(abs_path)
    finally:
        if db is None:
            connection.close()
