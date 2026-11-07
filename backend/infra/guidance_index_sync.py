"""Keep the workspace's real Git index in step with a guidance commit (issue #2322).

:func:`infra.guidance_git._commit` commits from a temporary ``GIT_INDEX_FILE`` and moves HEAD with
``update-ref``, so the person's own ``.git/index`` is a second, separate write that can fail on its own.
A stale ``index.lock`` makes every one of those syncs fail, the real index keeps the pre-save bytes, and
the person's next plain ``git commit`` writes those bytes back over the save. This module clears a
proven-stale lock through the one shared fail-closed rule, and finds the entries an earlier save left
behind so the next save brings them up too.

It never removes a lock itself and never writes the index: both callers' steps are returned as
``update-index`` arguments and run by :mod:`infra.guidance_git`, the only writer of guidance history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from loguru import logger

from infra.guidance_states import GitFault
from infra.project_repo_lock_recovery import GitIndexLockRecoveryError, recover_stale_git_index_lock

Git = Callable[..., str]
# One save repairs at most this many left-behind paths; the rest wait for the next save. The sweep
# bounds its own walk the same way (``guidance_git._SWEEP_LIMIT``) so a commit's subprocess count
# stays bounded whatever a person has staged.
_REPAIR_LIMIT = 100


def index_lock_fault(git: Git, repo: Path) -> str:
    """Clear a stale ``index.lock`` through the shared fail-closed rule before the real-index sync.

    Returns "" when there was nothing to clear or the lock is gone, and the rule's own fault text when
    it preserved a lock it could not prove stale. It never raises: this runs after ``update-ref``, so
    the commit has landed and a preserved lock must stay a fault in the result, never a failed save.
    """
    try:
        git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
        if not (git_dir / "index.lock").exists():
            return ""
        recover_stale_git_index_lock(git_dir)
    except (GitFault, GitIndexLockRecoveryError, OSError) as exc:
        logger.warning("guidance index lock in {} was preserved: {}", repo, exc)
        return str(exc)
    return ""


def left_behind_entries(git: Git, repo: Path, sha: str, skip: set[str]) -> list[tuple[str, tuple[str, ...]]]:
    """``update-index`` arguments bringing guidance entries an earlier save left behind up to ``sha``.

    A path qualifies only in the one state a save whose index sync failed leaves: its index entry
    differs from HEAD *and* the file on disk already holds HEAD's exact bytes. Everything else is left
    alone, because a person's own staging looks the same from one side only: their staged content is what
    the working-tree comparison rejects, a path missing from the index (their staged delete) or unmerged
    is theirs to finish, and a path HEAD does not have (their staged new file) is never invented. A
    deleted guidance file an earlier save left behind therefore keeps its existing reported remedy, and
    only writes are re-synced here. A read fault means nothing is known, so nothing is touched.
    """
    from core.guidance_paths import _guidance_shapes  # the sweep's own bound for guidance paths

    specs = [f":(glob){shape}" for shape in _guidance_shapes()]
    try:
        # A name holding a newline means the record did not split as ``-z`` promises: nothing is known
        # about it, so nothing is touched.
        behind = [
            rel
            for rel in _names(git(repo, "diff-index", "--cached", "--name-only", "-z", sha, "--", *specs))
            if rel not in skip and "\n" not in rel
        ]
        if not behind:
            return []
        head = _head_entries(git, repo, sha, sorted(behind))
        index = _index_entries(git, repo, sorted(behind))
    except GitFault as exc:
        logger.warning("guidance left-behind index scan skipped: {}", exc)
        return []
    repairs: list[tuple[str, tuple[str, ...]]] = []
    for rel in sorted(behind)[:_REPAIR_LIMIT]:
        entries, want = index.get(rel) or [], head.get(rel)
        if len(entries) != 1 or want is None:  # absent from the index, unmerged, or absent from HEAD
            continue
        (mode, blob, stage), = entries
        if stage != "0" or want == (mode, blob):
            continue
        if (repo / rel).exists() and _worktree_blob(git, repo, rel) == want[1]:
            repairs.append((rel, ("--add", "--cacheinfo", f"{want[0]},{want[1]},{rel}")))
    if len(behind) > _REPAIR_LIMIT:
        logger.warning("guidance left-behind index scan: {} paths behind, {} checked", len(behind), _REPAIR_LIMIT)
    return repairs


def _names(out: str) -> list[str]:
    return [name for name in out.split("\0") if name]


def _head_entries(git: Git, repo: Path, sha: str, paths: list[str]) -> dict[str, tuple[str, str]]:
    """``git ls-tree`` by path: the committed (mode, blob) of each path present in HEAD."""
    entries: dict[str, tuple[str, str]] = {}
    for record in _names(git(repo, "ls-tree", "-r", "-z", sha, "--", *paths)):
        info, _, name = record.partition("\t")
        fields = info.split()
        if len(fields) == 3:
            entries[name] = (fields[0], fields[2])
    return entries


def _index_entries(git: Git, repo: Path, paths: list[str]) -> dict[str, list[tuple[str, str, str]]]:
    """``git ls-files -s`` by path: every (mode, blob, stage) entry, so an unmerged path is visible."""
    entries: dict[str, list[tuple[str, str, str]]] = {}
    for record in _names(git(repo, "ls-files", "-s", "-z", "--", *paths)):
        info, _, name = record.partition("\t")
        fields = info.split()
        if len(fields) == 3:
            entries.setdefault(name, []).append((fields[0], fields[1], fields[2]))
    return entries


def _worktree_blob(git: Git, repo: Path, rel: str) -> str:
    """The raw bytes on disk as a blob id: the same unfiltered hash ``_commit`` wrote into HEAD.

    "" when Git cannot read the file at all. Nothing is known about it then, and the entry is left alone.
    """
    try:
        return git(repo, "hash-object", "--no-filters", "--", rel).strip()
    except GitFault:
        return ""
