"""One answer to "which registered file does this path name, inside this scope?".

``GET /api/files/revision`` and the chat's reference cards both ask it, so the
rule that a path must sit under a scope root (or be a read-only context file,
a workspace skill or a workspace role file), be a real file, and resolve to its
registered identity has a single home.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from core.context_source_paths import role_file_label
from core.role_context_sources import ROLE_ORDER
from core.skill_scanning import skill_files
from core.workspace_context_sources import is_read_only_context_path
from core.workspace_layout import resolve_workspace_root
from infra import config
from registry import service as registry_service


class FileLookup(NamedTuple):
    """What one path names. ``artifact`` is set only for an allowed, existing file."""

    path: Path | None
    allowed: bool
    is_file: bool
    artifact: dict[str, Any] | None


def shared_context_path(path: Path, *, db=None) -> bool:
    """Whether every scope reads ``path``: a read-only context file, or a workspace skill or role file.

    Only the files the loaders deliver to every scope qualify: a role file
    they read, and a ``SKILL.md`` in the workspace or a role's skill folder.
    Anything else under those folders stays with the workspace scope, as does
    saving any of them.
    """
    if is_read_only_context_path(path, config.WORKSPACE, db=db):
        return True
    root = resolve_workspace_root(config.WORKSPACE).resolve(strict=False)
    resolved = path.resolve(strict=False)
    if role_file_label(resolved, root) is not None:
        return True
    if resolved.name != "SKILL.md":
        return False
    skill_roots = (root, *(root / "context" / "roles" / role for role in ROLE_ORDER))
    return any(resolved in skill_files(skill_root) for skill_root in skill_roots)


def path_allowed(path: Path, bases: list[Path], *, db=None) -> bool:
    """Whether ``path`` is under a scope root or is a context file every scope reads."""
    return any(path == b or b in path.parents for b in bases) or shared_context_path(path, db=db)


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
    return FileLookup(
        candidate, True, True, registry_service.resolve_file_identity(str(candidate), db=db),
    )
