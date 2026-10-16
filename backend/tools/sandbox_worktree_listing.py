"""Fingerprint-validated listing of verified linked worktrees.

Listing a repository's ``.worktrees`` proves every entry's reciprocal Git
markers, which costs several ``resolve()`` calls and file reads per entry. Each
verdict is cached with the paths it consulted and every ancestor of each (so an
intermediate symlink swap is seen), and is reused only while all of those keep
the same identity and change times. A forged, edited, replaced, retargeted,
added or removed worktree is therefore verified again on the very next call.
There is no TTL.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

# repo -> {entry name: (stamped paths, verified root)}
_CACHE: dict[Path, dict[str, tuple[tuple, Path | None]]] = {}

Verify = Callable[[Path, Path, list[Path]], "Path | None"]
Memo = dict[Path, "tuple | None"]


def _stamp(path: Path, memo: Memo) -> tuple | None:
    """Identity stamp of ``path`` itself (lstat), shared per call via ``memo``."""
    if path in memo:
        return memo[path]
    try:
        st = os.lstat(path)
    except OSError:
        stamp = None
    else:
        stamp = (
            # A directory's times move whenever its contents do, so only its
            # identity and type matter; a symlink or file also keeps its times.
            (st.st_mode, st.st_ino, st.st_dev)
            if stat.S_ISDIR(st.st_mode)
            else (
                st.st_mode, st.st_ino, st.st_dev, st.st_size,
                st.st_mtime_ns, st.st_ctime_ns,
            )
        )
    memo[path] = stamp
    return stamp


def _stamped(deps: list[Path], memo: Memo) -> tuple:
    """Stamp every dep and each of its ancestors, once each."""
    paths = dict.fromkeys(p for dep in deps for p in (dep, *dep.parents))
    return tuple((p, _stamp(p, memo)) for p in paths)


def _unchanged(stamps: tuple, memo: Memo) -> bool:
    return all(_stamp(path, memo) == stamp for path, stamp in stamps)


def _verify_entry(entry: Path, repo: Path, verify: Verify) -> tuple:
    """Verify one entry and stamp what the verdict rests on.

    The verdict is computed twice around the stamps; if the consulted files
    moved in between, the stamps are withheld so the next call re-verifies
    instead of pinning a verdict to state it did not see.
    """

    def once() -> tuple[tuple, Path | None]:
        deps: list[Path] = [entry]
        root = verify(entry, repo, deps)
        if root != entry.resolve(strict=False):
            root = None
        return _stamped(deps, {}), root

    for _ in range(2):
        stamps, root = once()
        again, confirmed = once()
        if confirmed == root and again == stamps:
            return stamps, root
    return ((entry, object()),), root  # unstable: report it, never reuse it


def list_verified_entries(repo_root: Path, verify: Verify) -> list[Path]:
    """Return the sorted verified roots under ``<repo_root>/.worktrees``.

    ``verify(entry, repo, deps)`` returns the verified root of ``entry`` or
    ``None`` and appends every unresolved path its verdict reads to ``deps``.
    """
    repo = repo_root.expanduser().resolve(strict=False)
    parent = repo / ".worktrees"
    if not parent.is_dir():
        _CACHE.pop(repo, None)
        return []
    reusable = _CACHE.get(repo, {})
    records: dict[str, tuple] = {}
    worktrees: list[Path] = []
    memo: Memo = {}
    try:
        for entry in parent.iterdir():
            record = reusable.get(entry.name)
            if record is None or not _unchanged(record[0], memo):
                record = _verify_entry(entry, repo, verify)
            records[entry.name] = record
            if record[1] is not None:
                worktrees.append(record[1])
    except OSError:
        _CACHE.pop(repo, None)
        return []
    _CACHE[repo] = records
    return sorted(worktrees)
