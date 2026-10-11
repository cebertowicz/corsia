"""File identity resolution helpers for registry readers."""

from __future__ import annotations

from typing import Any

from infra.database import get_db
from tools.sandbox_config import get_allowed_dirs_rw


def find_file_identity(abs_path, *, db=None) -> dict[str, Any] | None:
    """Resolve a workspace/repository file logical-first, then by legacy path."""
    from registry import models
    from registry.service import prepare_file_identity

    identity = prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    _db = db or get_db()
    try:
        return models.find_identity_by_external_ids(
            "workspace",
            identity.artifact_type,
            (identity.external_id, identity.canonical),
            db=_db,
        )
    finally:
        if not db:
            _db.close()


def resolve_file_identities(abs_paths, *, db=None) -> dict[str, dict[str, Any]]:
    """Resolve active file owners for many physical paths in one registry read."""
    from registry.batch_projection import resolve_normalized_owners
    from registry.service import prepare_file_identity

    paths = list(dict.fromkeys(abs_paths))
    if not paths:
        return {}
    roots = get_allowed_dirs_rw(db=db)
    identities = {
        path: prepare_file_identity(path, db=db, allowed_roots=roots)
        for path in paths
    }
    candidates = [
        (identity.artifact_type, external_id)
        for identity in identities.values() if identity is not None
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


def resolve_file_identity(abs_path: str, *, db=None) -> dict[str, Any] | None:
    """Resolve an active file owner from a physical path."""
    from registry import models
    from registry.service import prepare_file_identity

    identity = prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    _db = db or get_db()
    try:
        for candidate in dict.fromkeys((identity.external_id, identity.canonical)):
            artifact = models.resolve(
                "workspace", identity.artifact_type, candidate, db=_db,
            )
            if artifact is not None:
                return artifact
        return None
    finally:
        if not db:
            _db.close()
