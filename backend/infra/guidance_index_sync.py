"""Record failed guidance index syncs and recover them under Git's index lock.

Guidance commits move HEAD separately from the real index. A failed index sync is
recorded only when the index still equals the pre-save entry. A recovery compares
that record with the current index and HEAD while holding Git's index lock.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
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


def _tree_entry(git: Git, repo: Path, revision: str | None, rel: str):
    if not revision:
        return None
    raw = git(repo, "ls-tree", "-z", revision, "--", f":(literal){rel}")
    if not raw:
        return None
    metadata, name = raw.rstrip("\0").split("\t", 1)
    mode, kind, blob = metadata.split()
    return [mode, blob] if name == rel and kind == "blob" else None


def _index_entry(git: Git, repo: Path, rel: str, env=None):
    raw = git(repo, "ls-files", "--stage", "-z", "--", f":(literal){rel}", env=env)
    entries = []
    for record in filter(None, raw.split("\0")):
        metadata, name = record.split("\t", 1)
        mode, blob, stage = metadata.split()
        if name == rel:
            entries.append((mode, blob, stage))
    if any(stage != "0" for _, _, stage in entries):
        return False, None
    return True, list(entries[0][:2]) if entries else None


def _records_path(git_dir: Path) -> Path:
    return git_dir / "corsia" / "unsynced-index.json"


def _read_records(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _write_records(path: Path, records) -> None:
    if not records:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="unsynced-index-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(records, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def record_failure(git: Git, repo: Path, rel: str, before: str | None, sha: str) -> None:
    """Record only an index entry equal to the parent of the failed save."""
    old = _tree_entry(git, repo, before, rel)
    valid, current = _index_entry(git, repo, rel)
    if not valid:
        return
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    path = _records_path(git_dir)
    records = _read_records(path)
    for record in records:
        if record["path"] == rel and record["to"] == old and record["from"] == current:
            record.update(to=_tree_entry(git, repo, sha, rel), commit=sha)
            _write_records(path, records)
            return
    if current == old:
        records.append({"path": rel, "from": old, "to": _tree_entry(git, repo, sha, rel), "commit": sha})
        _write_records(path, records)


def _take_index_lock(git_dir: Path):
    lock = git_dir / "index.lock"
    try:
        return os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        recover_stale_git_index_lock(git_dir)
        return os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)


def _consume_one(git: Git, repo: Path, git_dir: Path, record):
    lock = git_dir / "index.lock"
    fd = _take_index_lock(git_dir)
    owns_lock = True
    try:
        index = git_dir / "index"
        with tempfile.TemporaryDirectory(prefix="corsia-index-", dir=git_dir) as scratch:
            copy = Path(scratch) / "index"
            env = {"GIT_INDEX_FILE": str(copy)}
            if index.exists():
                shutil.copy2(index, copy)
                os.fchmod(fd, index.stat().st_mode & 0o777)
            else:
                git(repo, "read-tree", "--empty", env=env)
            rel = record["path"]
            valid, current = _index_entry(git, repo, rel, env=env)
            if not valid or current != record["from"]:
                return False, "index entry changed"
            if _tree_entry(git, repo, "HEAD", rel) != record["to"]:
                return False, "HEAD entry changed"
            target = record["to"]
            args = ("--add", "--cacheinfo", f"{target[0]},{target[1]},{rel}") if target else ("--force-remove", "--", rel)
            git(repo, "update-index", *args, env=env)
            with os.fdopen(fd, "wb", closefd=False) as stream, copy.open("rb") as source:
                shutil.copyfileobj(source, stream)
                stream.flush()
                os.fsync(fd)
            os.replace(lock, index)
            owns_lock = False
            return True, ""
    finally:
        os.close(fd)
        if owns_lock:
            lock.unlink(missing_ok=True)


def consume_records(git: Git, repo: Path, shown: set[str] | None = None):
    """Consume recorded failures, keeping any record a fault or held lock left unrepaired for a later attempt.
    Every outcome is logged; only those for ``shown`` paths (all when None) are returned to the caller."""
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    path = _records_path(git_dir)
    records = _read_records(path)
    kept, repaired, notes = [], [], []
    for number, record in enumerate(records):
        rel = record["path"]
        try:
            changed, reason = _consume_one(git, repo, git_dir, record)
        except (FileExistsError, GitIndexLockRecoveryError) as exc:
            notes.append((rel, f"Guidance index repair skipped {rel}: {exc}"))
            kept.extend(records[number:])
            break
        except (GitFault, OSError) as exc:
            notes.append((rel, f"Guidance index repair failed for {rel}: {exc}."))
            kept.append(record)
            continue
        if changed:
            old = record["from"]
            repaired.append((rel, f"{rel} (old staged blob: {old[1] if old else 'absent'})"))
        elif reason:
            notes.append((rel, f"Guidance index repair skipped {rel}: {reason}."))
    _write_records(path, kept)
    for _rel, note in notes:
        logger.warning("{}", note)
    if repaired:
        logger.info("Repaired guidance index: {}", ", ".join(line for _rel, line in repaired))
    return ([line for rel, line in repaired if shown is None or rel in shown],
            [line for rel, line in notes if shown is None or rel in shown])
