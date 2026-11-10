"""The workspace history's one record step, and the queue that runs it after a document save (C11).

:func:`record` commits saves in order under the history lock, each preceded by an outside version
(``Reason: unrecorded``, by an unknown editor) when the bytes it replaced are not what history holds.
A guidance save runs it before answering (:func:`infra.guidance_git.save_with_history`). A document save
writes its bytes, answers, and is recorded here afterwards (:func:`save_queued`): one workspace at a
time, in save order, consecutive saves of one document by one author under one message joined, the
person's ignore rules read once per batch so the save itself asks Git nothing. A version that cannot be recorded is kept and retried with the next save in that workspace, and the
next save of that document says so. A restore (``atomic``) queues behind every earlier save and
answers only once its own version is recorded, putting the old bytes back when it is not. A restore
refused as stale first records the outside edit that made it stale, so a reloaded list ends at that text.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from loguru import logger

from infra.guidance_cas import stale_result
from infra.guidance_history_paths import read_regular, write_bytes
from infra.guidance_messages import REASON_PREFIX, SAVE_FAILED, UNRECORDED_REASON
from infra.guidance_save_types import KnownHead, SaveResult, _Commit
from infra.guidance_states import GitFault

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="corsia-history")
_state = threading.Condition()
_pending: dict[Path, list[_Queued]] = {}
_running: set[Path] = set()
_retry: dict[Path, list[_Queued]] = {}
_unrecorded: dict[tuple[Path, str], str] = {}
QUEUED_FAILED = "History couldn't record an earlier save of {path} ({error}); it is retried with this save."


@dataclass(frozen=True)
class Version:
    """One save to record: each path's replaced bytes and new bytes (None: absent or deleted), who
    saved it, its commit message, and the kind (``guidance`` or ``document``) its outside version names."""

    files: dict[str, tuple[bytes | None, bytes | None]]
    author: str
    message: str
    kind: str
    tray: bool = False  # registered thread-folder files, judged by the ignore gate's tray rule


@dataclass(frozen=True)
class _Queued:
    version: Version
    done: Future | None = None  # a restore waits on its own version


def write_checked(writes: dict[Path, bytes | None], expected, write: Callable[[Path, bytes], object]):
    """Write ``writes`` (None deletes) unless a file changed since the caller read it; the stale result,
    or each path's replaced bytes. The check runs right before the write, wherever the write runs."""
    if (stale := stale_result(expected, read_bytes=read_regular)) is not None:
        return stale
    prior = {path: read_regular(path) for path in writes}
    for path, data in writes.items():
        path.unlink(missing_ok=True) if data is None else write(path, data)
    return prior


def put_back(prior: dict[Path, bytes | None], writes: dict[Path, bytes | None]) -> None:
    """Undo a write whose version was not recorded, wherever the file still holds what it wrote."""
    for path, old in prior.items():
        if read_regular(path) == writes[path]:
            write_bytes(path, old) if old is not None else path.unlink(missing_ok=True)


def record(repo: Path, versions: list[Version], git_dir: Path) -> tuple[list[_Commit | None], list[str], Exception | None]:
    """Under the history lock: commit ``versions`` in order, stopping at the first fault. Each landed
    version's commit (None: nothing changed), the lines to show, and the fault that stopped it."""
    from infra.guidance_git import _commit

    landed: list[_Commit | None] = []
    lines: list[str] = []
    try:
        known, blobs, blob_id = _read_head(repo, sorted({rel for version in versions for rel in version.files}), git_dir)
        for version in versions:
            if not version.files:  # every file it saved is one history keeps out
                landed.append(None)
                continue
            # Bytes history already holds were recorded by an earlier try; anything else it replaced
            # that history lacks came from outside CORSIA and is kept first, under no one's name.
            outside = {rel: prior for rel, (prior, data) in version.files.items()
                       if blob_id(prior) != blobs.get(rel) and (data is None or blob_id(data) != blobs.get(rel))}
            if outside:
                lines += _sweep(repo, outside, version.kind, known)
                blobs.update({rel: blob_id(prior) for rel, prior in outside.items()})
            files = {rel: data for rel, (_prior, data) in version.files.items()}
            landed.append(_commit(repo, files, version.message, version.author, known=known))
            blobs.update({rel: blob_id(data) for rel, data in files.items()})
    except Exception as exc:  # the caller decides what an unrecorded save means
        return landed, lines, exc
    return landed, lines, None


def sweep(repo: Path, rels: list[str], kind: str) -> list[str]:
    """Commit outside edits at ``rels`` from disk in one commit by an unknown editor."""
    return _sweep(repo, {rel: read_regular(repo / rel) for rel in rels}, kind)


def _sweep(repo: Path, files: dict[str, bytes | None], kind: str, known: KnownHead | None = None) -> list[str]:
    """Commit ``files`` as one outside version: nothing records who made them, and the last CORSIA
    writer did not write these bytes."""
    from infra.guidance_git import _OUTSIDE, _commit

    names = sorted(files)
    shown = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
    message = f"{kind} sweep: {shown}\n\n{REASON_PREFIX}{UNRECORDED_REASON}\nScope: sweep"
    done = _commit(repo, {rel: files[rel] for rel in names}, message, _OUTSIDE, repair_unchanged=False, known=known)
    if done is None:
        return []
    return ([f"Swept and committed {shown} as {done.sha[:7]} with Reason: unrecorded."] if done.changed else []) + done.unsynced_line()


def save_queued(
    repo: Path, writes: dict[Path, bytes | None], rels: dict[Path, str], *, author: str, message: str,
    expected, atomic: bool, write: Callable[[Path, bytes], object], tray: bool = False,
) -> SaveResult:
    """Blocking. Write a document save and queue its version; ``atomic`` waits for that version and
    puts the old bytes back when it is not recorded."""
    prior = write_checked(writes, expected, write)
    if isinstance(prior, SaveResult):
        if atomic and prior.outcome == "stale":
            _keep_outside(repo, on_disk(rels, author, message, "document", tray))
        return prior
    version = Version({rels[path]: (prior[path], data) for path, data in writes.items()}, author, message, "document", tray)
    done: Future | None = Future() if atomic else None
    line = _enqueue(repo, _Queued(version, done))
    if done is None:
        return SaveResult("queued", line=line)
    try:
        landed = done.result()  # every record step is bounded by the lock wait and Git's timeouts
    except Exception as exc:
        put_back(prior, writes)
        return SaveResult("failed", str(exc), line=SAVE_FAILED.format(error=exc))
    return SaveResult("written", sha=landed.sha) if landed is not None and landed.changed else SaveResult("unchanged")


def on_disk(rels: dict[Path, str], author: str, message: str, kind: str, tray: bool = False) -> Version:
    """Each file as it is on disk, over itself: recording it adds only the outside version of an edit
    history lacks, since its own bytes change nothing."""
    files = {rel: (data, data) for path, rel in rels.items() for data in [read_regular(path)]}
    return Version(files, author, message, kind, tray)


def _keep_outside(repo: Path, version: Version) -> None:
    """Blocking. Record the outside edit that made a restore stale before the refusal answers, so the
    history the person reloads ends at the text they see. A failure leaves the refusal as it is."""
    done: Future = Future()
    _enqueue(repo, _Queued(version, done))
    try:
        done.result()
    except Exception:
        logger.opt(exception=True).warning("history could not keep the outside edit behind a refused restore in {}", repo)


def wait_recorded(timeout: float = 60.0) -> bool:
    """Wait until nothing is queued or recording; False on timeout. Tests and shutdown use this."""
    with _state:
        return _state.wait_for(lambda: not (_pending or _running), timeout)


def _enqueue(repo: Path, queued: _Queued) -> str | None:
    """Queue ``queued`` behind every earlier save in ``repo`` (a failed one first); the line naming an
    earlier save of the same document that history could not record, if any."""
    rels = sorted(queued.version.files)
    with _state:
        errors = [(rel, error) for rel in rels if (error := _unrecorded.pop((repo, rel), None))]
        _pending.setdefault(repo, [])[:0] = _retry.pop(repo, [])
        _pending[repo].append(queued)
        start = repo not in _running
        _running.add(repo)
    if start:
        _POOL.submit(_drain, repo)
    return "\n".join(QUEUED_FAILED.format(path=rel, error=error) for rel, error in errors) or None


def _drain(repo: Path) -> None:
    """Record everything queued for one workspace, oldest first, until nothing is left."""
    while True:
        with _state:
            batch = _pending.pop(repo, [])
            if not batch:
                _running.discard(repo)
                _state.notify_all()
                return
        joined = coalesced(batch)
        landed, error = _record_locked(repo, [queued.version for queued in joined])
        for queued, commit in zip(joined, landed):
            with _state:  # a retried save that landed no longer needs its next save to say so
                for rel in queued.version.files:
                    _unrecorded.pop((repo, rel), None)
            if queued.done is not None:
                queued.done.set_result(commit)
        failed = joined[len(landed):]
        if error is not None and failed:
            logger.opt(exception=error).warning("history could not record {} saves in {}", len(failed), repo)
            kept = []
            for queued in failed:
                if queued.done is not None:  # the restore answers now and puts its old bytes back
                    queued.done.set_exception(error)
                else:
                    kept.append(queued)
            with _state:
                _retry[repo] = kept + _retry.get(repo, [])
                _unrecorded.update({(repo, rel): str(error) for queued in kept for rel in queued.version.files})
        with _state:
            _state.notify_all()


def _record_locked(repo: Path, versions: list[Version]) -> tuple[list[_Commit | None], Exception | None]:
    from infra.guidance_git import _acquire, _release, git_dir

    try:
        versions = _without_ignored(repo, versions)
        directory = git_dir(repo)
        handle = _acquire(repo, directory)[0]
    except Exception as exc:  # a busy lock or a Git fault: the whole batch waits for the next save
        return [], exc
    try:
        landed, _lines, error = record(repo, versions, directory)
    finally:
        _release(handle)
    return landed, error


def _without_ignored(repo: Path, versions: list[Version]) -> list[Version]:
    """Each version without the files the person's ignore rules keep out: one check per kind of file."""
    from infra.history_ignore import ignored

    out: set[tuple[bool, str]] = set()
    for tray in (False, True):
        if rels := sorted({rel for version in versions if version.tray == tray for rel in version.files}):
            out |= {(tray, rel) for rel in ignored(repo, rels, tray=tray)}
    return [replace(version, files={rel: pair for rel, pair in version.files.items() if (version.tray, rel) not in out})
            for version in versions] if out else versions


def coalesced(batch: list[_Queued]) -> list[_Queued]:
    """Join each save into the last earlier save of the same document when the same author wrote it
    under the same message straight over that save's bytes; the joined version keeps the earlier one's
    replaced bytes and place. A restore is never joined, so it stays its own version."""
    joined: list[_Queued] = []
    for queued in batch:
        version = queued.version
        last = next((index for index in range(len(joined) - 1, -1, -1)
                     if set(joined[index].version.files) & set(version.files)), None)
        earlier = joined[last].version if last is not None else None
        if (last is not None and earlier is not None and queued.done is None and joined[last].done is None
                and (earlier.author, earlier.message, set(earlier.files)) == (version.author, version.message, set(version.files))
                and all(version.files[rel][0] == earlier.files[rel][1] for rel in version.files)):
            files = {rel: (earlier.files[rel][0], data) for rel, (_prior, data) in version.files.items()}
            joined[last] = _Queued(replace(version, files=files))
        else:
            joined.append(queued)
    return joined


def _read_head(repo: Path, rels: list[str], git_dir: Path):
    """One ``rev-parse`` for the repository, its object format, HEAD and HEAD's tree, and one ``ls-tree``
    for the files' modes and blobs (an unborn HEAD has none); with the blob-id function for that format."""
    from infra.guidance_git import _git

    lines = _git(repo, "rev-parse", "--show-toplevel", "--show-object-format", "HEAD", "HEAD^{tree}",
                 ok=(0, 128)).splitlines()
    if len(lines) < 2 or Path(lines[0]).resolve(strict=False) != repo.resolve(strict=False):
        raise GitFault("the workspace Git directory resolves elsewhere")
    born = len(lines) == 4 and all(c in "0123456789abcdef" for c in lines[2])
    known = KnownHead(git_dir, lines[2] if born else None, lines[3] if born else None, {})
    blobs: dict[str, str] = {}
    if born and rels:
        raw = _git(repo, "ls-tree", "-z", "HEAD", "--", *(f":(literal){rel}" for rel in rels))
        for entry in filter(None, raw.split("\0")):
            metadata, name = entry.split("\t", 1)
            mode, kind, blob = metadata.split()
            if kind == "blob":
                known.modes[name], blobs[name] = mode, blob
    digest = hashlib.sha256 if lines[1] == "sha256" else hashlib.sha1

    def blob_id(data: bytes | None) -> str | None:
        """The object id Git gives ``data`` as a blob (None for no file), computed here, not by ``hash-object``."""
        return None if data is None else digest(b"blob %d\0" % len(data) + data).hexdigest()
    return known, blobs, blob_id
