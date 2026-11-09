"""Document versions, recorded after the save returns (C11).

A document save writes its bytes and returns; its version is committed here afterwards, one document
at a time in save order. Saves that queue behind a running record of the same document are coalesced
when one author made them back to back under one message and nothing changed the file between them.
The history lock is held only for the Git work, never across a write, a registration or an owner
report. A fault is logged and never reaches the save: the next save of that document finds the
unrecorded text and keeps it as an outside version (``guidance sweep:``), the same as an edit made
outside CORSIA. A restore (``atomic``) waits for the document's queue, then records in its own call
and puts the old bytes back when its version could not be recorded.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from loguru import logger

from infra.guidance_cas import stale_result
from infra.guidance_history_paths import is_empty_git_dir, read_regular, write_bytes
from infra.guidance_messages import REASON_PREFIX, SAVE_FAILED, UNRECORDED_REASON
from infra.guidance_save_types import SaveResult
from infra.guidance_states import GitFault, _LockBusy
from infra.guidance_targets import GuidanceTarget

MAX_DOCUMENT_BYTES = 1024 * 1024
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="corsia-document-history")
_state = threading.Condition()
_pending: dict[Path, list[_Version]] = {}
_running: set[Path] = set()


@dataclass(frozen=True)
class _Version:
    """One save to record: the bytes it wrote, the bytes it replaced (None for a new file), and who."""

    workspace: Path
    path: Path
    data: bytes
    prior: bytes | None
    author: str
    message: str
    registered: bool


def save_document(
    writes: dict[Path, bytes], *, workspace: Path, target: GuidanceTarget, author: str, message: str,
    expected: dict[Path, str | None] | None, atomic: bool, write: Callable[[Path, bytes], object],
) -> SaveResult:
    """Blocking. Write a document's bytes, then record them: queued, or for ``atomic`` before returning."""
    def text(data: bytes) -> bool:
        return len(data) <= MAX_DOCUMENT_BYTES and b"\0" not in data[:8192]
    if atomic:
        if not all(text(data) for data in writes.values()):
            return SaveResult("ordinary", "not text up to 1 MB")
        wait_recorded(set(writes))
    if (stale := stale_result(expected, read_bytes=read_regular)) is not None:
        return stale
    versions = []
    for path, data in writes.items():
        prior = read_regular(path)
        write(path, data)
        if text(data):
            versions.append(_Version(workspace, path, data, prior, author, message, target.registered))
    if not atomic:
        for version in versions:
            _enqueue(version)
        return SaveResult("queued")
    try:
        sha = _record(versions)
    except Exception as exc:  # nothing landed: put the old bytes back where the file still holds ours
        for version in versions:
            if read_regular(version.path) == version.data:
                write_bytes(version.path, version.prior) if version.prior is not None else version.path.unlink(missing_ok=True)
        return SaveResult("failed", str(exc), line=SAVE_FAILED.format(error=exc))
    return SaveResult("written", sha=sha) if sha else SaveResult("unchanged")


def wait_recorded(paths: set[Path] | None = None, timeout: float = 60.0) -> bool:
    """Wait until ``paths`` (every document when None) have nothing queued or recording; False on timeout."""
    def idle() -> bool:
        busy = set(_pending) | _running
        return not (busy if paths is None else busy & {path.resolve(strict=False) for path in paths})
    with _state:
        return _state.wait_for(idle, timeout)


def _enqueue(version: _Version) -> None:
    key = version.path.resolve(strict=False)
    with _state:
        _pending.setdefault(key, []).append(version)
        if key in _running:
            return
        _running.add(key)
    _POOL.submit(_drain, key)


def _drain(key: Path) -> None:
    """Record everything queued for one document, oldest first, until nothing is left."""
    while True:
        with _state:
            versions = _pending.pop(key, [])
            if not versions:
                _running.discard(key)
                _state.notify_all()
                return
        try:
            _record(coalesced(versions))
        except Exception:  # the save stands; the next save keeps this text as an outside version
            logger.opt(exception=True).warning("document version of {} not recorded", key)
        finally:
            with _state:
                _state.notify_all()


def coalesced(versions: list[_Version]) -> list[_Version]:
    """Join each save into the one before it when the same author wrote it under the same message
    straight over that save's bytes; the joined version keeps the first one's prior bytes."""
    joined: list[_Version] = []
    for version in versions:
        last = joined[-1] if joined else None
        if last is not None and (last.author, last.message) == (version.author, version.message) and version.prior == last.data:
            joined[-1] = replace(version, prior=last.prior)
        else:
            joined.append(version)
    return joined


def _record(versions: list[_Version]) -> str | None:
    """Commit ``versions`` (one workspace) in order under the history lock; the last landed sha, if any."""
    from infra.guidance_git import _OUTSIDE, _commit, _git, workspace_repository

    workspace = versions[0].workspace.resolve(strict=False)
    # A plain ``.git`` folder is its own Git directory; anything else is resolved (or created) by Git.
    git_dir: Path | None = workspace / ".git"
    if not (workspace / ".git").is_dir() or is_empty_git_dir(workspace / ".git"):
        repo, why = workspace_repository(workspace, create=True)
        if repo is None:
            raise GitFault(why or "no repository")
        git_dir = None
    rels = {version.path: version.path.resolve(strict=False).relative_to(workspace).as_posix() for version in versions}
    unregistered = sorted({rels[version.path] for version in versions if not version.registered})
    # Registration marks a thread file as kept, so only an unregistered document asks Git's ignore rules.
    ignored = set(_git(workspace, "check-ignore", "--", *unregistered, ok=(0, 1)).splitlines()) if unregistered else set()
    handle = _lock(workspace, git_dir)
    try:
        known, blobs, blob_id = _read_head(workspace, sorted(set(rels.values())))
        sha = None
        for version in versions:
            rel = rels[version.path]
            if rel in ignored:
                continue
            if version.prior is not None and blob_id(version.prior) != blobs.get(rel):
                landed = _commit(workspace, {rel: version.prior}, _sweep_message(rel), _OUTSIDE, repair_unchanged=False, known=known)
                blobs[rel] = blob_id(version.prior)
                sha = landed.sha if landed is not None and landed.changed else sha
            if blob_id(version.data) == blobs.get(rel):
                continue
            landed = _commit(workspace, {rel: version.data}, version.message, version.author, known=known)
            blobs[rel] = blob_id(version.data)
            sha = landed.sha if landed is not None and landed.changed else sha
        return sha
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _lock(workspace: Path, git_dir: Path | None) -> int:
    from infra.guidance_git import _acquire

    try:
        return _acquire(workspace, git_dir)[0]
    except _LockBusy as exc:
        raise GitFault(str(exc)) from exc


def _read_head(repo: Path, rels: list[str]):
    """One ``rev-parse`` for the repository, its object format, HEAD and HEAD's tree, and one ``ls-tree``
    for the documents' modes and blobs (an unborn HEAD has none); with the blob-id function for that format."""
    from infra.guidance_git import _git
    from infra.guidance_save_types import KnownHead

    lines = _git(repo, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--show-object-format",
                 "HEAD", "HEAD^{tree}", ok=(0, 128)).splitlines()
    if len(lines) < 3 or Path(lines[0]).resolve(strict=False) != repo.resolve(strict=False):
        raise GitFault("the workspace Git directory resolves elsewhere")
    born = len(lines) == 5 and all(c in "0123456789abcdef" for c in lines[3])
    known = KnownHead(Path(lines[1]), lines[3] if born else None, lines[4] if born else None, {})
    blobs: dict[str, str] = {}
    if born:
        raw = _git(repo, "ls-tree", "-z", "HEAD", "--", *(f":(literal){rel}" for rel in rels))
        for record in filter(None, raw.split("\0")):
            metadata, name = record.split("\t", 1)
            mode, kind, blob = metadata.split()
            if kind == "blob":
                known.modes[name], blobs[name] = mode, blob
    digest = hashlib.sha256 if lines[2] == "sha256" else hashlib.sha1

    def blob_id(data: bytes) -> str:
        """The object id Git gives ``data`` as a blob, computed here instead of by ``hash-object``."""
        return digest(b"blob %d\0" % len(data) + data).hexdigest()
    return known, blobs, blob_id


def _sweep_message(rel: str) -> str:
    return f"guidance sweep: {rel}\n\n{REASON_PREFIX}{UNRECORDED_REASON}\nScope: sweep"
