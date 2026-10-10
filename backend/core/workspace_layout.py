"""Canonical Workspace-owned project and thread layout resolution."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


# The folder under a workspace or project root that holds per-thread folders.
THREADS_DIR = ".threads"

# The errors ``Path.exists`` and ``Path.is_symlink`` read as "nothing there".
_ABSENT_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


def resolve_workspace_root(workspace: str | Path) -> Path:
    """Return the canonical supplied workspace root without global config lookup."""
    return Path(workspace).expanduser().resolve(strict=False)


def resolve_project_root(
    workspace: str | Path,
    project_id: str,
    *,
    db=None,
) -> Path | None:
    """Return the selected planning root for one project."""
    from core.project_context import resolve_project_paths

    root = resolve_workspace_root(workspace)
    _name, planning_root = resolve_project_paths(root, project_id, db=db)
    return planning_root


class ThreadRootResolver:
    """Validate ``.threads/<thread-id>`` destinations, inspecting each scope root once.

    A caller placing many threads under the same scope (a report over a whole project)
    shares one resolver, so the scope's canonical root and its ``.threads`` container are
    checked once instead of once per thread. Every filesystem call releases the GIL, which
    makes per-thread resolution convoy behind any other busy thread.
    """

    def __init__(self) -> None:
        self._scopes: dict[str, tuple[Path, str | None]] = {}

    def _container(self, scope_root: str | Path) -> tuple[Path, str | None]:
        key = str(scope_root)
        entry = self._scopes.get(key)
        if entry is None:
            threads_root = resolve_workspace_root(scope_root) / THREADS_DIR
            problem = None
            if threads_root.is_symlink():
                problem = f"thread layout container is a symlink: {threads_root}"
            elif threads_root.exists() and not threads_root.is_dir():
                problem = f"thread layout container is not a directory: {threads_root}"
            entry = self._scopes[key] = (threads_root, problem)
        return entry

    def root(self, scope_root: str | Path, thread_id: str) -> Path:
        """Return the validated canonical ``.threads/<thread-id>`` destination path."""
        identity = _validated_thread_id(thread_id)
        threads_root, problem = self._container(scope_root)
        if problem is not None:
            raise ValueError(problem)
        destination = threads_root / identity
        # The scope is canonical and neither the container nor the destination may be a
        # symlink, so the destination cannot resolve outside the scope.
        try:
            mode = os.lstat(destination).st_mode
        except OSError as error:
            if error.errno not in _ABSENT_ERRNOS:
                raise
            return destination
        if stat.S_ISLNK(mode):
            raise ValueError(f"thread destination is a symlink: {destination}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"thread destination is not a directory: {destination}")
        return destination


def resolve_thread_root(scope_root: str | Path, thread_id: str) -> Path:
    """Return a validated canonical ``.threads/<thread-id>`` destination path."""
    return ThreadRootResolver().root(scope_root, thread_id)


def resolve_thread_working_root(scope_root: str | Path, thread_id: str) -> Path:
    """Return the validated ``working`` tray for one canonical thread root."""
    working = resolve_thread_root(scope_root, thread_id) / "working"
    if working.is_symlink():
        raise ValueError(f"thread working root is a symlink: {working}")
    if working.exists() and not working.is_dir():
        raise ValueError(f"thread working root is not a directory: {working}")
    return working


def resolve_thread_uploads_root(scope_root: str | Path, thread_id: str) -> Path:
    """Return the validated durable ``uploads`` root for one thread."""
    uploads = resolve_thread_root(scope_root, thread_id) / "uploads"
    if uploads.is_symlink():
        raise ValueError(f"thread uploads root is a symlink: {uploads}")
    if uploads.exists() and not uploads.is_dir():
        raise ValueError(f"thread uploads root is not a directory: {uploads}")
    return uploads


def thread_tray_owners(path: str | Path) -> tuple[str, ...]:
    """Return every thread id whose private ``.threads/<id>/`` tray holds ``path``.

    Empty when the path is in no thread tray at all (a project/shared root, a
    repository, the scope root itself). More than one entry is only possible
    for a pathological nested layout, so a caller enforcing tray ownership must
    require *every* owner to be the acting thread — not just the outermost one.

    ``path`` should already be resolved: this inspects components only and does
    not touch the filesystem or follow symlinks.
    """
    parts = Path(path).parts
    return tuple(
        parts[index + 1]
        for index, part in enumerate(parts[:-1])
        if part == THREADS_DIR
    )


def _validated_thread_id(thread_id: str) -> str:
    """Validate an opaque thread identity before it becomes a path component."""
    if not isinstance(thread_id, str):
        raise ValueError("thread id must be a string")
    if (
        not thread_id.strip()
        or thread_id in {".", ".."}
        or "\x00" in thread_id
        or "/" in thread_id
        or "\\" in thread_id
    ):
        raise ValueError(f"unsafe thread id: {thread_id!r}")
    return thread_id
