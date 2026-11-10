"""The workspace history's one record step (C11).

:func:`record` commits saves in order under the history lock, each preceded by an outside version
(``Reason: unrecorded``, by an unknown editor) when the bytes it replaced are not what history holds.
Every save runs it before answering (:func:`infra.guidance_git.save_with_history`), guidance and
documents alike, so nothing sits between a save's write and its commit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from infra.guidance_cas import stale_result
from infra.guidance_history_paths import read_regular, write_bytes
from infra.guidance_messages import REASON_PREFIX, UNRECORDED_REASON
from infra.guidance_save_types import KnownHead, _Commit
from infra.guidance_states import GitFault


@dataclass(frozen=True)
class Version:
    """One save to record: each path's replaced bytes and new bytes (None: absent or deleted), who
    saved it, its commit message, and the kind (``guidance`` or ``document``) its outside version names."""

    files: dict[str, tuple[bytes | None, bytes | None]]
    author: str
    message: str
    kind: str


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
