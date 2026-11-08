"""Keep the workspace's real Git index in step with a guidance commit (issue #2322).

:func:`infra.guidance_git._commit` commits from a temporary ``GIT_INDEX_FILE`` and moves HEAD with
``update-ref``, so the person's own ``.git/index`` is a second, separate write that can fail on its own.
A stale ``index.lock`` makes every one of those syncs fail, the real index keeps the pre-save bytes, and
the person's next plain ``git commit`` writes those bytes back over the save. This module clears a
proven-stale lock through the one shared fail-closed rule, so the save's own sync can run.

It never removes a lock outside that rule and never writes the index: :mod:`infra.guidance_git` stays
the only writer of guidance history. A deliberately staged version matching a parent in the CORSIA
run, including an earlier CORSIA save or a preceding human commit, is indistinguishable from a failed
sync: the index is reset to HEAD while the disk and HEAD bytes remain unchanged.
A concurrent stage can be overwritten between the repair check and write, or after a save moves HEAD and before its index sync.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from loguru import logger

from infra.guidance_states import GitFault
from infra.project_repo_lock_recovery import GitIndexLockRecoveryError, recover_stale_git_index_lock

Git = Callable[..., str]


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


def startup_candidates(git: Git, repo: Path, workspace: Path, db) -> list[str]:
    """Find index differences only within the existing guidance path specs."""
    from core.guidance_paths import classify_guidance_path, sweep_pathspecs

    specs = sweep_pathspecs(workspace, db)
    head = git(repo, "rev-parse", "--verify", "-q", "HEAD", ok=(0, 1)).strip()
    candidates = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", head, "--", *specs).split("\0") if head and specs else []
    return [rel for rel in candidates if rel and classify_guidance_path(repo / rel, workspace=workspace, db=db)]


def _tree_entry(git: Git, repo: Path, revision: str, rel: str):
    raw = git(repo, "ls-tree", "-z", revision, "--", f":(literal){rel}")
    if not raw:
        return None
    metadata, _name = raw.rstrip("\0").split("\t", 1)
    mode, kind, blob = metadata.split()
    return (mode, blob) if kind == "blob" else None


def _previous_blobs(git: Git, repo: Path, rel: str):
    """Yield parent entries in the uninterrupted path-specific CORSIA run."""
    history = git(repo, "log", "--first-parent", "--format=%H%x00%ce", "HEAD", "--", f":(literal){rel}")
    for line in history.splitlines():
        commit, email = line.split("\0", 1)
        if email != "corsia@localhost":
            break
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
        yield _tree_entry(git, repo, parents[1], rel) if len(parents) > 1 else None


def recovery_args(git: Git, repo: Path, rel: str):
    """Return update-index arguments with the old blob, or why the path is preserved."""
    head = _tree_entry(git, repo, "HEAD", rel)
    raw = git(repo, "ls-files", "--stage", "-z", "--", f":(literal){rel}")
    entries = []
    for record in filter(None, raw.split("\0")):
        metadata, _name = record.split("\t", 1)
        mode, blob, stage = metadata.split()
        entries.append((mode, blob, stage))
    if any(stage != "0" for _, _, stage in entries):
        return None, "unmerged index entry"
    index = (entries[0][0], entries[0][1]) if entries else None
    if index == head:
        return None, ""
    if index is not None and head is not None and index[1] == head[1]:
        return None, "index blob matches HEAD; staged mode preserved"
    if head is None:
        return None, "path is absent from HEAD"
    path = repo / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None, "disk path is not a regular file"
        disk_mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        disk_blob = git(repo, "hash-object", "--stdin", data=path.read_bytes()).strip()
    except OSError as exc:
        raise GitFault(f"cannot read {rel}: {exc}") from exc
    if (disk_mode, disk_blob) != head:
        return None, "disk differs from HEAD"
    parents = _previous_blobs(git, repo, rel)
    if not any((index is None and parent is None) or (index is not None and parent is not None and index[1] == parent[1]) for parent in parents):
        return None, "index does not match a CORSIA save parent"
    return (("--add", "--cacheinfo", f"{head[0]},{head[1]},{rel}"), index[1] if index else None), ""
