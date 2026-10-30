"""Guidance-history helpers backed by the canonical Git runner."""
from __future__ import annotations

import hashlib
from pathlib import Path

# Appended to a file's last commit when its working copy differs: two drafts can share it.
LOCAL_EDIT_MARK = "·dirty"


def digest(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def is_empty_git_dir(path: Path) -> bool:
    """Whether ``path`` is the empty ``.git`` a Linux shell launch leaves where it could not init; Git discovery skips it."""
    try:
        return path.is_dir() and not path.is_symlink() and not any(path.iterdir())
    except OSError:
        return False


def history_git(workspace: Path, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
    """Run one read-only Git command in the workspace's own guidance history; a fault raises."""
    from infra.guidance_git import GitFault, _git_bytes, workspace_repository

    repo, why = workspace_repository(workspace, create=False)
    if repo is None:
        raise GitFault(why or "workspace guidance history unavailable")
    return _git_bytes(repo, *args, ok=ok)


def find_commit(repo: Path, trailer: str, path: Path) -> str | None:
    from infra.guidance_git import _git

    found = _git(repo, "log", "-1", "--format=%H", "--fixed-strings", f"--grep={trailer}", "--", str(path), ok=(0, 128))
    return found.strip() or None


def file_version(repo: Path, path: Path) -> str | None:
    from infra.guidance_git import _changed, _git

    rel = path.resolve(strict=False).relative_to(repo.resolve(strict=False)).as_posix()
    commit = _git(repo, "log", "-1", "--format=%H", "--", f":(literal){rel}").strip()
    if not commit:
        return None
    return commit + (LOCAL_EDIT_MARK if _changed(repo, [f":(literal){rel}"]) else "")
