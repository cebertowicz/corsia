"""Ownership checks for project source/ entries."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import uuid
from pathlib import Path

from core.workspace_scaffolding import atomic_rename_noreplace


@dataclass(frozen=True)
class SourceEntryIdentity:
    """Filesystem identity of one source entry owned by a mutation."""

    entry: Path
    kind: str
    device: int
    inode: int
    raw_target: Path | None


class SourceEntryChangedError(RuntimeError):
    """A source entry no longer matches the object owned by the mutation."""


def capture_source_entry(entry: Path, kind: str) -> SourceEntryIdentity:
    """Capture a source entry before exposing it to later mutation stages."""
    stat = entry.lstat()
    raw_target = entry.readlink() if entry.is_symlink() else None
    return SourceEntryIdentity(
        entry=entry,
        kind=kind,
        device=stat.st_dev,
        inode=stat.st_ino,
        raw_target=raw_target,
    )


def _entry_matches(identity: SourceEntryIdentity, entry: Path) -> bool:
    try:
        stat = entry.lstat()
    except FileNotFoundError:
        return False
    if (stat.st_dev, stat.st_ino) != (identity.device, identity.inode):
        return False
    if identity.kind == "linked":
        return entry.is_symlink() and entry.readlink() == identity.raw_target
    return not entry.is_symlink() and entry.is_dir()


def require_source_entry(identity: SourceEntryIdentity) -> None:
    """Fail when the public source path no longer names the owned object."""
    if not _entry_matches(identity, identity.entry):
        raise SourceEntryChangedError(
            f"repository source binding '{identity.entry.name}' changed"
        )


def _restore_claim(claim: Path, entry: Path) -> None:
    try:
        atomic_rename_noreplace(claim, entry)
    except FileExistsError as exc:
        raise RuntimeError(
            f"repository source binding '{entry.name}' changed; both its new "
            f"public entry and the prior replacement quarantined at {claim} "
            "were preserved"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"repository source binding '{entry.name}' changed; its replacement "
            f"was preserved at {claim} because atomic restoration failed: {exc}"
        ) from exc


def remove_owned_source_entry(
    identity: SourceEntryIdentity,
    *,
    missing_ok: bool = False,
) -> None:
    """Remove only the exact source object represented by ``identity``."""
    claim = identity.entry.with_name(
        f".{identity.entry.name}.corsia-claim-{uuid.uuid4().hex}"
    )
    try:
        atomic_rename_noreplace(identity.entry, claim)
    except FileNotFoundError:
        if missing_ok:
            return
        raise SourceEntryChangedError(
            f"repository source binding '{identity.entry.name}' disappeared"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            f"repository source binding '{identity.entry.name}' could not be "
            f"claimed atomically; source and claim state were preserved: {exc}"
        ) from exc
    if not _entry_matches(identity, claim):
        _restore_claim(claim, identity.entry)
        raise SourceEntryChangedError(
            f"repository source binding '{identity.entry.name}' changed"
        )
    if identity.kind == "linked":
        claim.unlink()
    else:
        shutil.rmtree(claim)


def publish_source_entry(
    staged_identity: SourceEntryIdentity,
    public_entry: Path,
) -> SourceEntryIdentity:
    """Publish one private staged entry without replacing an existing entry."""
    try:
        atomic_rename_noreplace(staged_identity.entry, public_entry)
    except FileExistsError as exc:
        remove_owned_source_entry(staged_identity)
        raise ValueError(
            f"A repo named '{public_entry.name}' already exists in source/"
        ) from exc
    return SourceEntryIdentity(
        entry=public_entry,
        kind=staged_identity.kind,
        device=staged_identity.device,
        inode=staged_identity.inode,
        raw_target=staged_identity.raw_target,
    )
