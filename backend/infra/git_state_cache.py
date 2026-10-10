"""Freshness-keyed, process-local cache for per-checkout Git answers.

An answer is reused only while ``os.lstat`` of every file the Git command
reads is unchanged: device, inode, size, mtime and ctime, so a rewrite that
restores the mtime still changes the key. There is no TTL. Anything that
cannot be keyed (a symlinked or unreadable input, a Git environment override,
an unreadable or unlistable config chain) is computed fresh every time.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

_MAX_ENTRIES = 256
_ABSENT = "absent"
# Config files Git may read that are not listed by --show-origin when absent.
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
_entries: OrderedDict[tuple, tuple[tuple[tuple[str, ...], tuple], object]] = OrderedDict()


class _Uncacheable(Exception):
    """An input cannot be keyed, so the answer must be computed fresh."""


def clear_git_state_cache() -> None:
    """Drop every cached answer."""
    with _lock:
        _entries.clear()


def _sig(path: str | Path, *, optional: bool = False, identity: bool = False):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if optional:
            return _ABSENT
        raise _Uncacheable from None
    except OSError:
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
        _sig(git_dir),
        _sig(git_dir / "HEAD"),
        _sig(git_dir / "config"),
        _sig(git_dir / "commondir", optional=True),
        _sig(git_dir / "reftable", optional=True),
        _sig(git_dir / "objects"),
        _sig(git_dir / "refs"),
        tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith("GIT_") or k in ("HOME", "XDG_CONFIG_HOME"))),
        (git, _sig(os.path.realpath(git))),
        tuple((path, _sig(path, optional=True)) for path in candidates),
    )


def _config_files(repo: Path) -> tuple[str, ...]:
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
    files = {
        os.path.join(repo, origin[len("file:"):])  # the repo-local origin is relative
        for line in result.stdout.splitlines()
        if (origin := line.split("\t", 1)[0]).startswith("file:")
    }
    return tuple(sorted(files))


def _extra_sigs(files: tuple[str, ...]) -> tuple:
    return tuple((path, _sig(path)) for path in files)


def cached_git_answer(
    repo: Path,
    kind: str,
    compute: Callable[[], T],
    *,
    cache_if: Callable[[T], bool] = lambda _answer: True,
) -> T:
    """Return ``compute()``, reusing a prior answer while its inputs are unchanged.

    Exceptions and answers that ``cache_if`` rejects are never stored, so a
    failure or a denial is recomputed on every call.
    """
    try:
        base = _base_key(repo, kind)
        with _lock:
            entry = _entries.get(base)
            if entry is not None:
                _entries.move_to_end(base)
        if entry is not None:
            (cached_files, cached_sigs), answer = entry
            if _extra_sigs(cached_files) == cached_sigs:
                return answer  # type: ignore[return-value]
        files = _config_files(repo)
        before = _extra_sigs(files)
    except _Uncacheable:
        return compute()
    answer = compute()
    if cache_if(answer):
        try:
            if _base_key(repo, kind) == base and _extra_sigs(files) == before:
                with _lock:
                    _entries[base] = ((files, before), answer)
                    _entries.move_to_end(base)
                    while len(_entries) > _MAX_ENTRIES:
                        _entries.popitem(last=False)
        except _Uncacheable:
            pass
    return answer
