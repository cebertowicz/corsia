"""Guidance-history helpers backed by the canonical Git runner."""
from __future__ import annotations

import hashlib
from pathlib import Path

# Appended to a file's last commit when its working copy differs: two drafts can share it.
LOCAL_EDIT_MARK = "·dirty"


def digest(content: str | bytes | None) -> str | None:
    """The one compare-and-swap hash: a file's content as the bytes it holds.

    Text is its own bytes (``str.encode``), so a caller that read a file's bytes and one that holds
    its text agree, while a read that turned CRLF into LF does not claim to be the file."""
    if content is None:
        return None
    return hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest()


def bytes_before_text(path: Path) -> bytes | None:
    """The bytes to compare-and-swap against, read before the caller reads the file's text.

    A caller builds its new content from that text read and passes :func:`digest` of these bytes, so a
    change made after this read refuses the write instead of being overwritten by content built from
    older text. Text is never the expectation: a text read turns CRLF and a lone CR into LF, which is not
    what the destination holds. ``None`` is the file's absence.
    """
    return path.read_bytes() if path.exists() else None


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


def commit_parent(workspace: Path, sha: str) -> str | None:
    """The first parent of one recorded guidance commit; None for a root commit."""
    out = history_git(workspace, "rev-parse", "--verify", "-q", f"{sha}^", ok=(0, 1))
    return out.decode().strip() or None


def committed_bytes(workspace: Path, commit: str, rel: str) -> bytes | None:
    """One workspace-relative path's exact bytes at a recorded commit; None when it has none."""
    oid = history_git(workspace, "rev-parse", "--verify", "-q", f"{commit}:{rel}", ok=(0, 1))
    return history_git(workspace, "cat-file", "blob", oid.decode().strip()) if oid.strip() else None


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
