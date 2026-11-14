"""Guidance-history helpers backed by the canonical Git runner."""
from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime, timezone
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


def write_bytes(path: Path, data: bytes) -> None:
    """Write guidance bytes, creating their parent directory when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_regular(path: Path) -> bytes | None:
    """A regular file's bytes, or None when absent; a symlink or other file type raises.

    It is also the compare-and-swap expectation: a caller reads these bytes before the file's text and
    passes :func:`digest` of them, so a change made after this read refuses the write instead of being
    overwritten by content built from older text. Text is never the expectation: a text read turns CRLF
    and a lone CR into LF, which is not what the destination holds."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read()


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


# Slack below ``since`` for a clock stepped back between the stored stamp and the commit.
_SINCE_SLACK_SECONDS = 3600


def find_commit(repo: Path, trailer: str, path: Path, since: str | None = None) -> str | None:
    """The newest commit on ``path`` whose message carries ``trailer``; None when there is none.

    A miss walks history back to ``since`` (a stored UTC timestamp, naive or aware), or all of it
    when ``since`` is None. Each commit walked costs a tree diff, so an unbounded miss grows with
    the history and can outlast the Git timeout."""
    from infra.guidance_git import _git

    bound = []
    if since is not None:
        stamp = datetime.fromisoformat(since)
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        bound = [f"--since=@{int(stamp.timestamp()) - _SINCE_SLACK_SECONDS}"]
    found = _git(
        repo, "log", "-1", "--format=%H", *bound, "--fixed-strings", f"--grep={trailer}", "--", str(path),
        ok=(0, 128),
    )
    return found.strip() or None


def file_version(repo: Path, path: Path) -> str | None:
    from infra.guidance_git import _changed, _git

    rel = path.resolve(strict=False).relative_to(repo.resolve(strict=False)).as_posix()
    commit = _git(repo, "log", "-1", "--format=%H", "--", f":(literal){rel}").strip()
    if not commit:
        return None
    return commit + (LOCAL_EDIT_MARK if _changed(repo, [f":(literal){rel}"]) else "")
