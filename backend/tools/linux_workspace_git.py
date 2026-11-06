"""Linux shell: make sure the workspace ``.git`` exists so the shell can hide the workspace's Git history.

The backend runs Git in the workspace outside the sandbox, and agents reach its history only
through the workspace commit and history tools. The shell hides the marker, git dir and common
dir. bubblewrap cannot mask an absent path without creating it on the host, and an absent
``.git`` would let a shell plant a repository the backend then adopts, so the backend makes
one before the mounts are built.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from loguru import logger

from tools.workspace_git_paths import workspace_git_history_paths


def _refuse(path: Path, cause: str) -> RuntimeError:
    return RuntimeError(f'Couldn’t protect the workspace Git path {path}, so the command did not run. {cause}')


def _refuse_fault(path: Path, exc: OSError) -> RuntimeError:
    """The refusal for an OS failure; the raw error goes to the log."""
    logger.warning('Workspace Git path {}: {}', path, exc)
    return _refuse(path, 'CORSIA can’t write there. Check the folder’s permissions and free disk space, then run the command again.')


def _require_plain(path: Path) -> None:
    """An existing path must be a regular file or directory; a symlink could be a planted redirect."""
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise _refuse_fault(path, exc) from exc
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise _refuse(path, 'It is a symlink or special file. Replace it with a regular file, then run the command again.')


def _make_empty_git(marker: Path) -> None:
    """Make an empty ``.git`` directory. Git discovery skips it, so an enclosing repository still works."""
    try:
        os.mkdir(marker, 0o755)
    except FileExistsError:
        _require_plain(marker)
    except OSError as exc:
        raise _refuse_fault(marker, exc) from exc


def prepare_workspace_git(workspace: Path | None) -> tuple[Path, ...]:
    """Make sure the workspace ``.git`` exists, then return ``workspace_git_history_paths``.

    With no ``.git`` the backend makes the repository a first guidance save makes; if that
    doesn't happen (an enclosing repository, or init fails) it makes an empty ``.git`` instead.
    A ``.git`` file with no ``gitdir:`` line yields the marker alone, so only it is hidden.
    A symlinked ``.git`` refuses the launch before any Git runs. No Git process runs when
    ``.git`` exists.
    """
    if workspace is None:
        return ()
    marker = workspace.resolve(strict=False) / '.git'
    if marker.is_symlink():
        # The sandbox mounts the link's target, so a shell could swap the link itself for a planted repository.
        raise _refuse(marker, 'It is a symlink. Replace it with the directory it points at, or with a gitdir: file.')
    if not os.path.lexists(marker):
        from infra.guidance_git import workspace_repository

        repo, reason = workspace_repository(marker.parent, create=True)
        if repo is None:
            logger.warning('Workspace history stays off at {}: {}', marker.parent, reason)
            _make_empty_git(marker)
    return workspace_git_history_paths(workspace)
