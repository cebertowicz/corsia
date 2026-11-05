"""Linux shell: make the workspace Git control paths exist so the read-only overlay covers them.

bubblewrap cannot overlay an absent path without creating it on the host. The backend runs
Git in the workspace outside the sandbox, so a repository, ``commondir`` or ``config.worktree``
planted by a shell would run with the backend's rights. The backend therefore creates each
absent path before the mounts are built.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from loguru import logger

from tools.workspace_git_paths import workspace_git_paths


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


def _create_once(path: Path, content: str) -> None:
    """Create ``path`` only if absent and never follow a symlink; racing launches both succeed."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o644)
    except FileExistsError:
        _require_plain(path)
        return
    except OSError as exc:
        raise _refuse_fault(path, exc) from exc
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(content)
    except OSError as exc:
        raise _refuse_fault(path, exc) from exc


def _make_empty_git(marker: Path) -> None:
    """Make an empty ``.git`` directory. Git discovery skips it, so an enclosing repository still works."""
    try:
        os.mkdir(marker, 0o755)
    except FileExistsError:
        _require_plain(marker)
    except OSError as exc:
        raise _refuse_fault(marker, exc) from exc


def prepare_workspace_git(workspace: Path | None) -> tuple[Path, ...]:
    """Create every absent workspace Git control path, then return ``workspace_git_paths``.

    With no ``.git`` the backend makes the repository a first guidance save makes; if that
    doesn't happen (an enclosing repository, or init fails) it makes an empty ``.git`` instead.
    A git dir without ``HEAD`` gets no placeholders, so the mount plan freezes that directory:
    ``.git`` for the empty fallback, the workspace root for a ``.git`` file with no ``gitdir:`` line.
    A symlinked ``.git`` refuses the launch before any Git runs. No Git process runs when
    ``.git`` exists. Existing files are never overwritten; a write
    that fails raises, so a command never runs with a control path open.
    """
    paths = workspace_git_paths(workspace)
    if not paths:
        return paths
    if paths[0].is_symlink():
        # The sandbox mounts the link's target, so a shell could swap the link itself for a planted repository.
        raise _refuse(paths[0], 'It is a symlink. Replace it with the directory it points at, or with a gitdir: file.')
    if not os.path.lexists(paths[0]):
        from infra.guidance_git import workspace_repository

        repo, reason = workspace_repository(paths[0].parent, create=True)
        if repo is None:
            logger.warning('Workspace history stays off at {}: {}', paths[0].parent, reason)
            _make_empty_git(paths[0])
        paths = workspace_git_paths(workspace)
    _marker, _config, _lock, worktree_config, commondir, _hooks = paths
    git_dir = commondir.parent
    if not (git_dir / 'HEAD').is_file():
        # An empty fallback marker, or one that points nowhere: there is no git directory to place
        # placeholders in, and the absent control paths freeze that directory.
        return paths
    # A linked worktree's git dir always has commondir; a standalone or separate one gets ".",
    # which Git reads as the git dir itself.
    _create_once(commondir, '.\n')
    _create_once(worktree_config, '')
    return workspace_git_paths(workspace)
