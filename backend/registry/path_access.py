"""One answer to "which registered file does this path name, inside this scope?".

``GET /api/files/revision`` and the chat's reference cards both ask it, so the
rule that a path must sit under a scope root (or be a workspace context file),
be a real file, and resolve to its registered identity has a single home.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from core.workspace_context_sources import is_workspace_context_path
from infra import config
from registry import service as registry_service


class FileLookup(NamedTuple):
    """What one path names. ``artifact`` is set only for an allowed, existing file."""

    path: Path | None
    allowed: bool
    is_file: bool
    artifact: dict[str, Any] | None


def path_allowed(path: Path, bases: list[Path], *, db=None) -> bool:
    """Whether ``path`` is under a scope root or is a workspace context file."""
    return (
        any(path == b or b in path.parents for b in bases)
        or is_workspace_context_path(path, config.WORKSPACE, db=db)
    )


def lookup_files(raw_paths, bases: list[Path], *, db=None) -> dict[str, FileLookup]:
    """Resolve several paths and their registered identities in one registry read."""
    candidates: dict[str, FileLookup] = {}
    existing: list[str] = []
    for raw_path in dict.fromkeys(raw_paths):
        try:
            candidate = Path(raw_path).expanduser().resolve()
        except OSError:
            candidates[raw_path] = FileLookup(None, False, False, None)
            continue
        if not path_allowed(candidate, bases, db=db):
            candidates[raw_path] = FileLookup(candidate, False, False, None)
            continue
        if not candidate.is_file():
            candidates[raw_path] = FileLookup(candidate, True, False, None)
            continue
        candidates[raw_path] = FileLookup(candidate, True, True, None)
        existing.append(str(candidate))
    identities = registry_service.resolve_file_identities(existing, db=db)
    return {
        raw_path: FileLookup(lookup.path, lookup.allowed, lookup.is_file, identities.get(str(lookup.path)))
        for raw_path, lookup in candidates.items()
    }


def lookup_file(raw_path: str, bases: list[Path], *, db=None) -> FileLookup:
    """Resolve a path to its file and registered identity within the scope."""
    try:
        candidate = Path(raw_path).expanduser().resolve()
    except OSError:
        return FileLookup(None, False, False, None)
    if not path_allowed(candidate, bases, db=db):
        return FileLookup(candidate, False, False, None)
    if not candidate.is_file():
        return FileLookup(candidate, True, False, None)
    return FileLookup(
        candidate, True, True, registry_service.resolve_file_identity(str(candidate), db=db),
    )
