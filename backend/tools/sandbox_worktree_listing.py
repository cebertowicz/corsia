"""Fingerprint-validated listing of verified linked worktrees.

Listing a repository's ``.worktrees`` verifies every entry's reciprocal Git
markers, which is O(worktrees) filesystem work. Each verdict is cached together
with the paths it consulted and reused only while every one of them keeps the
same identity, size and change times, so a forged, edited, replaced, added or
removed worktree is verified again on the very next call. There is no TTL.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

# repo -> (anchor fingerprint, {entry name: (deps, fingerprint, verified root)}).
_CACHE: dict[Path, tuple[tuple, dict[str, tuple]]] = {}

Verify = Callable[[Path, Path, list[Path]], "Path | None"]


def _stat_stamp(path: Path) -> tuple | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        # A directory's times move whenever its contents do; its identity and
        # type are what a verdict depends on (symlink swap, replacement).
        return (st.st_mode, st.st_ino, st.st_dev)
    return (
        st.st_mode, st.st_ino, st.st_dev, st.st_size,
        st.st_mtime_ns, st.st_ctime_ns,
    )


def _fingerprint(deps: list[Path]) -> tuple:
    return tuple(_stat_stamp(dep) for dep in deps)


def _verify_entry(entry: Path, repo: Path, verify: Verify) -> tuple:
    """Verify one entry and record what the verdict rests on.

    The verdict is computed twice around the fingerprint; if the consulted
    files moved in between, the fingerprint is withheld so the next call
    re-verifies instead of pinning a verdict to state it did not see.
    """

    def once() -> tuple[list[Path], Path | None]:
        deps: list[Path] = [entry]
        root = verify(entry, repo, deps)
        if root != entry.resolve(strict=False):
            root = None
        return list(dict.fromkeys(deps)), root

    for _ in range(2):
        deps, root = once()
        fingerprint = _fingerprint(deps)
        again, confirmed = once()
        if confirmed == root and _fingerprint(again) == fingerprint:
            return deps, fingerprint, root
    # Unstable: report the latest verdict but never match a later fingerprint.
    return deps, (object(),), root


def list_verified_entries(repo_root: Path, verify: Verify) -> list[Path]:
    """Return the sorted verified roots under ``<repo_root>/.worktrees``.

    ``verify(entry, repo, deps)`` returns the verified root of ``entry`` or
    ``None`` and appends every path its verdict depends on to ``deps``.
    """
    repo = repo_root.expanduser().resolve(strict=False)
    parent = repo / ".worktrees"
    if not parent.is_dir():
        _CACHE.pop(repo, None)
        return []
    # The directories the per-entry verdicts resolve through must keep their
    # identity; anything else invalidates every cached verdict.
    anchor = _fingerprint([repo, parent, repo / ".git" / "worktrees"])
    prior = _CACHE.get(repo)
    reusable = prior[1] if prior is not None and prior[0] == anchor else {}
    records: dict[str, tuple] = {}
    worktrees: list[Path] = []
    try:
        for entry in parent.iterdir():
            record = reusable.get(entry.name)
            if record is None or _fingerprint(record[0]) != record[1]:
                record = _verify_entry(entry, repo, verify)
            records[entry.name] = record
            if record[2] is not None:
                worktrees.append(record[2])
    except OSError:
        _CACHE.pop(repo, None)
        return []
    _CACHE[repo] = (anchor, records)
    return sorted(worktrees)
