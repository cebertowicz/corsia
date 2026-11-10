"""Which saved files outside guidance are recorded documents in the workspace history.

A recorded document is a text file in the workspace that the person keeps: anything outside the
thread trays, and a file in a tray only once it is registered as an artifact. Never recorded: the
ephemeral ``working`` folders (a tray's own and the workspace's top-level one), a file inside another
repository (a project's source checkout), a path the workspace's Git ignores (the writer's check), and
anything not text up to 1 MB (:func:`is_history_text`, the one text rule history keeps). Guidance is
classified first by :mod:`core.guidance_paths`.
"""

from __future__ import annotations

from pathlib import Path

from core.context_source_paths import _PROJECTS_DIR, _THREADS_DIR, _absolute
from core.guidance_paths import _real_file

MAX_HISTORY_BYTES = 1024 * 1024
_WORKING_DIR = "working"


def is_history_text(data: bytes | None, size: int | None = None) -> bool:
    """Whether history records these bytes: text (no NUL in its first 8 KB) up to 1 MB; ``size`` is the
    whole file's when ``data`` is only its start, and None (a delete) always counts."""
    return data is None or ((len(data) if size is None else size) <= MAX_HISTORY_BYTES and b"\0" not in data[:8192])


def _tray_parts(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    """The path below a thread tray (``.threads/<id>/``) at workspace or project level, else None."""
    if len(parts) >= 2 and parts[0] == _PROJECTS_DIR:
        parts = parts[2:]
    if len(parts) >= 3 and parts[0] == _THREADS_DIR:
        return parts[2:]
    return None


def in_thread_folder(path: str | Path, workspace: str | Path) -> bool:
    """Whether ``path`` sits in a thread tray, so only its registration makes it a recorded document."""
    absolute, root = _absolute(path), _absolute(workspace)
    if absolute is None or root is None or not absolute.is_relative_to(root):
        return False
    return _tray_parts(absolute.relative_to(root).parts) is not None


def _inside_other_repository(absolute: Path, root: Path) -> bool:
    """A ``.git`` entry in any folder between the workspace and the file: that repository owns it."""
    folder = absolute.parent
    while folder != root and folder.is_relative_to(root):
        if (folder / ".git").exists():
            return True
        folder = folder.parent
    return False


def _is_text(absolute: Path) -> bool:
    try:
        with absolute.open("rb") as handle:
            return is_history_text(handle.read(8192), absolute.stat().st_size)
    except OSError:
        return False


def _ephemeral(parts: tuple[str, ...]) -> bool:
    """A path in a ``working`` folder the thread trim owns (a tray's) or the workspace's top-level one;
    a person's own folder that happens to be named ``working`` is kept."""
    tray = _tray_parts(parts)
    return (parts[0] == _WORKING_DIR and len(parts) > 1) or (tray is not None and len(tray) > 1 and tray[0] == _WORKING_DIR)


def is_history_document(path: str | Path, *, workspace: str | Path, db) -> bool:
    """Whether a save of ``path`` is recorded as a document version (Git's ignore rules are the caller's check)."""
    absolute, root = _absolute(path), _absolute(workspace)
    if absolute is None or root is None or not absolute.is_relative_to(root):
        return False
    parts = absolute.relative_to(root).parts
    if not parts or ".git" in parts or _ephemeral(parts):
        return False
    # A file this save creates is judged by the bytes it writes; the writer records text only.
    if not _real_file(absolute, root) or (absolute.exists() and not (absolute.is_file() and _is_text(absolute))):
        return False
    if _inside_other_repository(absolute, root):
        return False
    if _tray_parts(parts) is not None:
        from registry import service as registry_service

        return registry_service.resolve_file_identity(str(absolute), db=db) is not None
    return True
