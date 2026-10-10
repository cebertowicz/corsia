"""Canonical, failure-compensating workspace and project scaffolds."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import sys
import uuid

from core.scaffold_layouts import _Layout, _workspace_layout, _project_layout


@dataclass(frozen=True)
class _OwnedEntry:
    path: Path
    kind: str
    device: int
    inode: int


@dataclass(frozen=True)
class ScaffoldOwnership:
    """Filesystem identities installed by one scaffold operation."""

    entries: tuple[_OwnedEntry, ...]


class AtomicRenameUnavailableError(RuntimeError):
    """The current host/filesystem cannot publish without replacement."""


class ScaffoldCompensationError(RuntimeError):
    """A failed scaffold could not remove every entry it still owns."""


def atomic_rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename sibling entries without replacing ``destination``."""
    source_parent = source.parent.resolve(strict=True)
    destination_parent = destination.parent.resolve(strict=True)
    if source_parent != destination_parent:
        raise ValueError("atomic no-replace rename requires sibling paths")

    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(source_parent, parent_flags)
    try:
        atomic_rename_noreplace_at(parent_fd, source.name, destination.name)
    finally:
        os.close(parent_fd)


def atomic_rename_noreplace_at(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    """Atomically rename two entries through one already-bound parent fd."""
    if (
        Path(source_name).name != source_name
        or Path(destination_name).name != destination_name
    ):
        raise ValueError("atomic no-replace rename requires leaf names")
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:
            raise AtomicRenameUnavailableError(
                "atomic no-replace rename is unavailable on this macOS host"
            ) from exc
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise AtomicRenameUnavailableError(
                "atomic no-replace rename is unavailable on this Linux host"
            ) from exc
        flags = 0x00000001  # RENAME_NOREPLACE
    else:
        raise AtomicRenameUnavailableError(
            f"atomic no-replace rename is unsupported on {sys.platform}"
        )
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = rename(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination_name)
    if error == errno.ENOENT:
        raise FileNotFoundError(error, os.strerror(error), source_name)
    unsupported = {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
    }
    if error in unsupported:
        raise AtomicRenameUnavailableError(
            "atomic no-replace rename is unsupported by this filesystem"
        )
    raise OSError(
        error,
        os.strerror(error),
        f"{source_name} -> {destination_name}",
    )


def scaffold_workspace(root: Path) -> Path:
    """Install the canonical workspace layout without overwriting user files."""
    target, _ownership = _install(Path(root).expanduser(), _workspace_layout())
    return target


def scaffold_project(
    root: Path,
    *,
    name: str,
    description: str = "",
    with_ownership: bool = False,
) -> Path | tuple[Path, ScaffoldOwnership]:
    """Install the canonical project layout without overwriting user files."""
    target, ownership = _install(
        Path(root).expanduser(),
        _project_layout(name, description),
    )
    return (target, ownership) if with_ownership else target


def remove_owned_scaffold(ownership: ScaffoldOwnership) -> None:
    """Remove only unchanged filesystem identities installed by one scaffold."""
    failures: list[str] = []
    for entry in reversed(ownership.entries):
        try:
            _remove_owned_entry(entry)
        except (OSError, RuntimeError) as error:
            failures.append(f"{entry.path}: {error}")
    if failures:
        raise ScaffoldCompensationError(
            "scaffold compensation incomplete: " + "; ".join(failures)
        )


def _install(target: Path, layout: _Layout) -> tuple[Path, ScaffoldOwnership]:
    if target.exists() or target.is_symlink():
        return target, _merge_existing(target, layout)
    return target, _stage_and_publish(target, layout)


def _validate_existing_target(target: Path, layout: _Layout) -> None:
    if target.is_symlink() or not target.is_dir():
        raise ValueError(f"Scaffold target must be a directory: {target}")
    for relative in layout.directories:
        _validate_candidate(target / relative, "directory")
    for relative, _content in layout.files:
        _validate_candidate(target / relative, "file")


def _validate_candidate(candidate: Path, kind: str) -> None:
    if candidate.is_symlink():
        raise ValueError(f"Canonical scaffold entries cannot be symlinks: {candidate}")
    if not candidate.exists():
        return
    expected = candidate.is_dir() if kind == "directory" else candidate.is_file()
    if not expected:
        raise ValueError(f"Canonical scaffold {kind} has the wrong type: {candidate}")


def _stage_and_publish(target: Path, layout: _Layout) -> ScaffoldOwnership:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.scaffold-{uuid.uuid4().hex}")
    staged.mkdir()
    try:
        _create_layout(staged, layout)
        ownership = _capture_layout(staged, target, layout)
        try:
            atomic_rename_noreplace(staged, target)
        except FileExistsError:
            _cleanup_private(staged)
            return _merge_existing(target, layout)
        except BaseException as error:
            if _entry_matches(ownership.entries[0], target):
                _compensate_or_raise(error, ownership)
            _cleanup_private(staged)
            raise
        return ownership
    except BaseException:
        if staged.exists() or staged.is_symlink():
            _cleanup_private(staged)
        raise


def _create_layout(target: Path, layout: _Layout) -> None:
    for relative in layout.directories:
        (target / relative).mkdir()
    for relative, content in layout.files:
        (target / relative).write_text(content, encoding="utf-8")


def _capture_layout(staged: Path, target: Path, layout: _Layout) -> ScaffoldOwnership:
    entries = [_capture_entry(staged, target, "directory")]
    entries.extend(
        _capture_entry(staged / relative, target / relative, "directory")
        for relative in layout.directories
    )
    entries.extend(
        _capture_entry(staged / relative, target / relative, "file")
        for relative, _content in layout.files
    )
    return ScaffoldOwnership(entries=tuple(entries))


def _merge_existing(target: Path, layout: _Layout) -> ScaffoldOwnership:
    _validate_existing_target(target, layout)
    owned: list[_OwnedEntry] = []
    try:
        for relative in layout.directories:
            _publish_missing(target / relative, "directory", None, owned)
        for relative, content in layout.files:
            _publish_missing(target / relative, "file", content, owned)
        _validate_existing_target(target, layout)
    except BaseException as error:
        _compensate_or_raise(
            error,
            ScaffoldOwnership(entries=tuple(owned)),
        )
    return ScaffoldOwnership(entries=tuple(owned))


def _publish_missing(
    destination: Path,
    kind: str,
    content: str | None,
    owned: list[_OwnedEntry],
) -> None:
    if destination.exists() or destination.is_symlink():
        _validate_candidate(destination, kind)
        return
    private = destination.with_name(f".{destination.name}.scaffold-{uuid.uuid4().hex}")
    try:
        if kind == "directory":
            private.mkdir()
        else:
            private.write_text(content or "", encoding="utf-8")
        identity = _capture_entry(private, destination, kind)
        try:
            atomic_rename_noreplace(private, destination)
        except FileExistsError:
            _cleanup_private(private)
            _validate_candidate(destination, kind)
            return
        except BaseException:
            if _entry_matches(identity, destination):
                owned.append(identity)
            elif private.exists() or private.is_symlink():
                _cleanup_private(private)
            raise
        owned.append(identity)
    except BaseException:
        if private.exists() or private.is_symlink():
            _cleanup_private(private)
        raise


def _capture_entry(source: Path, destination: Path, kind: str) -> _OwnedEntry:
    stat = source.lstat()
    return _OwnedEntry(destination, kind, stat.st_dev, stat.st_ino)


def _entry_matches(identity: _OwnedEntry, path: Path) -> bool:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    if (stat.st_dev, stat.st_ino) != (identity.device, identity.inode):
        return False
    return (
        path.is_dir() and not path.is_symlink()
        if identity.kind == "directory"
        else path.is_file() and not path.is_symlink()
    )


def _remove_owned_entry(identity: _OwnedEntry) -> None:
    if not (identity.path.exists() or identity.path.is_symlink()):
        return
    claim = identity.path.with_name(
        f".{identity.path.name}.scaffold-claim-{uuid.uuid4().hex}"
    )
    atomic_rename_noreplace(identity.path, claim)
    if not _entry_matches(identity, claim):
        _restore_claim(claim, identity.path)
        raise RuntimeError("entry identity changed before compensation")
    try:
        claim.rmdir() if identity.kind == "directory" else claim.unlink()
    except OSError:
        _restore_claim(claim, identity.path)
        raise


def _restore_claim(claim: Path, destination: Path) -> None:
    try:
        atomic_rename_noreplace(claim, destination)
    except Exception as error:
        raise RuntimeError(
            f"could not restore preserved scaffold entry from {claim}: {error}"
        ) from error


def _compensate_or_raise(error: BaseException, ownership: ScaffoldOwnership) -> None:
    try:
        remove_owned_scaffold(ownership)
    except ScaffoldCompensationError as cleanup_error:
        raise RuntimeError(
            f"{error}; scaffold compensation failed: {cleanup_error}"
        ) from error
    raise error


def _cleanup_private(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
