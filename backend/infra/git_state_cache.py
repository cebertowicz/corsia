"""Freshness-keyed, process-local cache for per-checkout Git answers.

An answer is reused only while ``os.lstat`` of every input the Git command
reads is unchanged: files that are read by content are keyed by device,
inode, size, mtime and ctime (so a rewrite that restores the mtime still
changes the key), and directories that are only required to exist are keyed
by identity (device, inode, owner, mode) so routine index refreshes, fetches
and object writes do not invalidate them. The Git environment, the ``git``
binary and every config file Git lists for the checkout are part of the key.
There is no TTL. Anything that cannot be keyed (a symlinked or unreadable
input, ``core.worktree``, a ``.git`` that is not a directory, an unlistable
config chain) is computed fresh on every call, as is every error and denial.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

_MAX_ENTRIES = 256
_ABSENT = "absent"
# Config files Git may read that --show-origin cannot list while they are absent.
_CONFIG_CANDIDATES = (
    "/etc/gitconfig",
    "/opt/homebrew/etc/gitconfig",
    "/usr/local/etc/gitconfig",
    "/Library/Developer/CommandLineTools/usr/etc/gitconfig",
    "/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig",
    "/usr/share/git-core/gitconfig",
    "/Applications/Xcode.app/Contents/Developer/usr/etc/gitconfig",
)
_lock = threading.Lock()
_entries: OrderedDict[tuple, tuple[tuple, Any]] = OrderedDict()


class _Uncacheable(Exception):
    """An input cannot be keyed, so the answer must be computed fresh."""


def clear_git_state_cache() -> None:
    """Drop every cached answer."""
    with _lock:
        _entries.clear()


def _sig(path: str | Path, *, optional: bool = False, identity: bool = False):
    try:
        st = os.lstat(path)
    except OSError as exc:
        if optional and exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return _ABSENT
        raise _Uncacheable from None
    if stat.S_ISLNK(st.st_mode):
        raise _Uncacheable
    if identity:
        return (st.st_dev, st.st_ino, st.st_uid, st.st_mode)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_uid, st.st_mode)


def _base_key(repo: Path, kind: str) -> tuple:
    git_dir = repo / ".git"
    git = shutil.which("git")
    if git is None:
        raise _Uncacheable
    home = os.environ.get("HOME", "")
    xdg = os.environ.get("XDG_CONFIG_HOME") or (f"{home}/.config" if home else "")
    candidates = [*_CONFIG_CANDIDATES, f"{home}/.gitconfig", f"{xdg}/git/config"]
    candidates += [os.environ[name] for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM") if name in os.environ]
    return (
        kind,
        str(repo),
        _sig(repo, identity=True),
        _sig(git_dir, identity=True),
        _sig(git_dir / "objects", identity=True),
        _sig(git_dir / "refs", identity=True),
        _sig(git_dir / "HEAD"),
        _sig(git_dir / "config"),
        _sig(git_dir / "config.worktree", optional=True),
        _sig(git_dir / "commondir", optional=True),
        _sig(git_dir / "reftable", optional=True),
        tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith("GIT_") or k in ("HOME", "XDG_CONFIG_HOME"))),
        (git, _sig(os.path.realpath(git))),
        tuple((path, _sig(path, optional=True)) for path in candidates),
    )


def _config_files(repo: Path) -> tuple[tuple[str, bool], ...]:
    """List every file Git reads for config here, including included ones."""
    try:
        result = subprocess.run(
            ["git", "config", "--show-origin", "--list", "--includes"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise _Uncacheable from None
    if result.returncode != 0:
        raise _Uncacheable
    files: set[str] = set()
    for line in result.stdout.splitlines():
        origin, _, setting = line.partition("\t")
        if setting.partition("=")[0].lower() == "core.worktree":
            raise _Uncacheable  # the worktree path is resolved outside the keyed files
        if origin.startswith("file:"):
            files.add(os.path.join(repo, origin[len("file:"):]))  # the repo-local origin is relative
    return tuple((path, False) for path in sorted(files))


def _extra_sigs(extras: tuple[tuple[str, bool], ...]) -> tuple:
    return tuple((path, _sig(path, optional=optional)) for path, optional in extras)


def _store(base: tuple, extras: tuple, sigs: tuple, answer: Any) -> None:
    with _lock:
        _entries[base] = ((extras, sigs), answer)
        _entries.move_to_end(base)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)


def cached_git_answer(
    repo: Path,
    kind: str,
    compute: Callable[[], T],
    *,
    cache_if: Callable[[T], bool] = lambda _answer: True,
    answer_paths: Callable[[], list[Path]] | None = None,
) -> T:
    """Return ``compute()``, reusing a prior answer while its inputs are unchanged.

    ``answer_paths`` names extra optional paths the answer depends on; it runs
    before ``compute`` so the answer is keyed to the state it was computed from.
    Exceptions and answers that ``cache_if`` rejects are never stored.
    """
    try:
        base = _base_key(repo, kind)
        with _lock:
            entry = _entries.get(base)
            if entry is not None:
                _entries.move_to_end(base)
        if entry is not None:
            (cached_extras, cached_sigs), answer = entry
            if _extra_sigs(cached_extras) == cached_sigs:
                return answer  # type: ignore[no-any-return]
        extras = _config_files(repo)
        if answer_paths is not None:
            extras += tuple((str(path), True) for path in answer_paths())
        before = _extra_sigs(extras)
    except _Uncacheable:
        return compute()
    answer = compute()
    if cache_if(answer):
        try:
            if _base_key(repo, kind) == base and _extra_sigs(extras) == before:
                _store(base, extras, before, answer)
        except _Uncacheable:
            pass
    return answer


def _branch_answer_paths(repo: Path) -> list[Path]:
    """Paths whose existence changes how ``symbolic-ref --short`` abbreviates HEAD."""
    git_dir = repo / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        raise _Uncacheable from None
    name = head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else ""
    parts = Path(name).parts
    if not name or Path(name).is_absolute() or ".." in parts or "." in parts:
        raise _Uncacheable
    # Unambiguous shortening tries these rules; each can shadow the short name.
    return [
        git_dir / name,
        git_dir / "refs" / name,
        git_dir / "refs" / "tags" / name,
        git_dir / "refs" / "remotes" / name,
        git_dir / "refs" / "remotes" / name / "HEAD",
        git_dir / "packed-refs",
    ]


def cached_current_branch(repo: Path, compute: Callable[[Path], str]) -> str:
    """Return the checkout's current branch name, as ``compute(repo)`` would."""
    return cached_git_answer(repo, "current-branch", lambda: compute(repo), answer_paths=lambda: _branch_answer_paths(repo))


def cached_layout_error(repo: Path, compute: Callable[..., str | None], *args: Any) -> str | None:
    """Return the Git layout verdict of ``compute(repo, *args)``; only "no error" is cached."""
    return cached_git_answer(repo, "layout", lambda: compute(repo, *args), cache_if=lambda error: error is None)
