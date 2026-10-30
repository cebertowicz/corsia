"""One answer to "which registered file does this path name, inside this scope?".

Chat reference cards use this lookup. Document reads and the revision route share
``path_allowed`` for scope checks, with their own identity and read semantics.
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
    has_identity: bool = False


def path_allowed(path: Path, bases: list[Path], *, db=None) -> bool:
    """Whether ``path`` is under a scope root or is a workspace context file."""
    return (
        any(path == b or b in path.parents for b in bases)
        or is_workspace_context_path(path, config.WORKSPACE, db=db)
    )


class ReadRefused(Exception):
    """Why ``checked_read`` refused a path: ``missing``, ``outside`` the scope, or ``not_file``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


def checked_read(raw_path: str, bases: list[Path], *, db=None) -> Path:
    """The one read-scope gate: ``raw_path`` as it resolves now, an existing file inside ``bases``.

    Every read of a file's bytes passes through here (a document open, raw image
    serving, the image a region comment is on), so the answers cannot differ.
    """
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReadRefused("missing", str(exc)) from exc
    if not path_allowed(path, bases, db=db):
        raise ReadRefused("outside")
    if not path.is_file():
        raise ReadRefused("not_file")
    return path


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
    identity = registry_service.prepare_file_identity(str(candidate), db=db)
    if identity is None:
        return FileLookup(candidate, True, True, None)
    artifact = registry_service.resolve_file_identity(identity, db=db)
    has_identity = artifact is not None or registry_service.find_file_identity(identity, db=db) is not None
    return FileLookup(candidate, True, True, artifact, has_identity)
