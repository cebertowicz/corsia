"""Candidate discovery for the worktree reaper.

Only entries directly under ``<repo>/.worktrees/`` are ever considered. The
base checkout, linked worktrees elsewhere (serving checkouts) and the worktree
holding the running backend are never candidates. The idle clock is read from
the filesystem before any Git call, so probing cannot reset it.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

from infra.worktree_reaper_probe import WorktreeEntry, admin_dir, list_worktrees
from tools.sandbox_config import repo_worktree_root

_UUID_PREFIX = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:-|$)"
)
_SHORT_PREFIX = re.compile(r"^([0-9a-f]{8})(?:-|$)")
_ADMIN_FILES = ("HEAD", "index", "logs/HEAD", "ORIG_HEAD")
_BACKEND_FILE = Path(__file__).resolve()


@dataclass(frozen=True)
class Candidate:
    """One verified linked worktree under ``.worktrees/``."""

    name: str
    path: Path
    admin: Path
    idle_since: float
    owner_thread: str | None
    entry: WorktreeEntry | None


@dataclass(frozen=True)
class Orphan:
    """One directory under ``.worktrees/`` that Git does not verify."""

    name: str
    path: Path
    has_git_entry: bool
    registered: bool
    symlink: bool


def owner_thread(name: str) -> str | None:
    """Return the owning thread id or short id encoded in a worktree name."""
    for pattern in (_UUID_PREFIX, _SHORT_PREFIX):
        match = pattern.match(name)
        if match:
            return match.group(1)
    return None


def _mtime(path: Path) -> float | None:
    try:
        return path.lstat().st_mtime
    except OSError:
        return None


def _admin_branch(admin: Path) -> str | None:
    try:
        head = (admin / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def idle_since(repo: Path, worktree: Path, admin: Path) -> float:
    """Return the newest activity mtime for one worktree, without Git."""
    paths = [admin / name for name in _ADMIN_FILES]
    branch = _admin_branch(admin)
    if branch:
        paths.append(repo / ".git" / "logs" / "refs" / "heads" / branch)
    paths.append(worktree)
    times = [value for value in map(_mtime, paths) if value is not None]
    return max(times) if times else 0.0


def newest_tree_mtime(root: Path) -> float | None:
    """Return the newest lstat mtime anywhere under ``root``, or None if unreadable."""
    newest = _mtime(root)
    if newest is None:
        return None
    failed: list[OSError] = []
    for current, dirs, files in os.walk(root, onerror=failed.append):
        for name in (*dirs, *files):
            value = _mtime(Path(current) / name)
            if value is None:
                return None
            newest = max(newest, value)
    return None if failed else newest


def is_backend_checkout(path: Path) -> bool:
    """Return whether the running backend's own source lives under ``path``."""
    return _BACKEND_FILE == path or path in _BACKEND_FILE.parents


def scan_repo(repo: Path) -> tuple[list[Candidate], list[Orphan], list[WorktreeEntry]]:
    """Return verified candidates, unverified orphans and Git's registrations."""
    parent = repo / ".worktrees"
    if not parent.is_dir() or parent.is_symlink():
        return [], [], []
    verified: list[tuple[str, Path, Path, float]] = []
    unverified: list[Path] = []
    for entry in sorted(parent.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            if entry.is_symlink():
                unverified.append(entry)
            continue
        path = entry.resolve(strict=False)
        if is_backend_checkout(path):
            continue
        admin = admin_dir(path)
        if repo_worktree_root(path, repo) == path and admin is not None:
            # The idle clock is read before the first Git call below.
            verified.append((entry.name, path, admin, idle_since(repo, path, admin)))
        else:
            unverified.append(entry)
    registrations = list_worktrees(repo)
    by_path = {item.path: item for item in registrations}
    candidates = [
        Candidate(name, path, admin, idle, owner_thread(name), by_path.get(path))
        for name, path, admin, idle in verified
    ]
    orphans = [
        Orphan(
            name=entry.name,
            path=entry,
            has_git_entry=os.path.lexists(entry / ".git"),
            registered=entry.resolve(strict=False) in by_path,
            symlink=entry.is_symlink(),
        )
        for entry in unverified
    ]
    return candidates, orphans, registrations
