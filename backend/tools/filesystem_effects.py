"""Descriptor-bound filesystem effects.

Authorization resolves a canonical absolute path. These helpers bind that path
to directory descriptors before the effect so an ancestor cannot be replaced
with a symlink between authorization and the syscall.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fnmatch
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import IO
from uuid import uuid4

from core.workspace_scaffolding import atomic_rename_noreplace_at
from tools.host_path_policy import credential_paths_beneath


class FilesystemBindingError(PermissionError):
    """The authorized path could not be bound safely for an effect."""


_BRACE_GROUP = re.compile(r"\{([^{}]*,[^{}]*)\}")
_MAX_BRACE_EXPANSIONS = 64


def expanded_globs(pattern: str) -> tuple[str, ...]:
    """Expand brace alternation into the plain globs it stands for (bounded)."""
    pending = [pattern]
    expanded: list[str] = []
    while pending:
        candidate = pending.pop()
        match = _BRACE_GROUP.search(candidate)
        if match is None:
            expanded.append(candidate)
            continue
        alternatives = match.group(1).split(",")
        if len(pending) + len(expanded) + len(alternatives) > _MAX_BRACE_EXPANSIONS:
            return ()
        pending.extend(
            candidate[: match.start()] + alternative + candidate[match.end() :]
            for alternative in alternatives
        )
    return tuple(expanded)


def _match_relative_glob(relative: Path, pattern: str) -> bool:
    pattern_parts = tuple(
        part for part in pattern.replace("\\", "/").split("/") if part
    )
    path_parts = relative.parts

    @lru_cache(maxsize=None)
    def matches(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return matches(pattern_index + 1, path_index) or (
                path_index < len(path_parts)
                and matches(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and matches(pattern_index + 1, path_index + 1)
        )

    return matches(0, 0)


def match_relative_glob(relative: Path, pattern: str) -> bool:
    """Match a root-relative path with ``**`` and brace alternation."""
    return any(
        _match_relative_glob(relative, expanded)
        for expanded in expanded_globs(pattern)
    )


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC


def _require_descriptor_support() -> None:
    required_dir_fd = (os.open, os.mkdir, os.stat)
    if (
        not _NOFOLLOW
        or not _DIRECTORY
        or any(function not in os.supports_dir_fd for function in required_dir_fd)
    ):
        raise FilesystemBindingError(
            "This platform cannot safely bind the authorized filesystem path."
        )


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory without following any path component."""
    _require_descriptor_support()
    if not path.is_absolute():
        raise FilesystemBindingError("Authorized filesystem path is not absolute.")

    current_fd = os.open("/", _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        raise FilesystemBindingError(
            f"Authorized path changed before the filesystem effect: {path}"
        ) from exc


@dataclass(frozen=True)
class BoundParent:
    """A no-follow descriptor for an authorized path's parent directory."""

    fd: int
    leaf: str
    path: Path

    def open_file(self, flags: int, *, mode: int = 0o666) -> int:
        try:
            return os.open(
                self.leaf,
                flags | _NOFOLLOW | _CLOEXEC,
                mode,
                dir_fd=self.fd,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise FilesystemBindingError(
                f"Authorized path changed before the filesystem effect: {self.path}"
            ) from exc


@contextmanager
def bound_parent(
    path: Path,
    *,
    create_parents: bool = False,
) -> Iterator[BoundParent]:
    """Bind an authorized file path to its no-follow parent descriptor."""
    if not path.name:
        raise FilesystemBindingError("A file path is required for this effect.")
    parent_fd = _open_directory(path.parent, create=create_parents)
    try:
        yield BoundParent(fd=parent_fd, leaf=path.name, path=path)
    finally:
        os.close(parent_fd)


def _validate_regular_file(fd: int, path: Path, *, writable: bool) -> os.stat_result:
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise FilesystemBindingError(f"Authorized path is not a regular file: {path}")
    if writable and file_stat.st_nlink != 1:
        raise FilesystemBindingError(
            f"Refusing to modify a multiply-linked file: {path}"
        )
    return file_stat


@contextmanager
def open_bound_text(
    path: Path,
    *,
    writable: bool = False,
) -> Iterator[tuple[IO[str], os.stat_result]]:
    """Open one existing regular file through a bound parent descriptor."""
    flags = os.O_RDWR if writable else os.O_RDONLY
    with bound_parent(path) as parent:
        fd = parent.open_file(flags)
        try:
            file_stat = _validate_regular_file(fd, path, writable=writable)
            mode = "r+" if writable else "r"
            with os.fdopen(fd, mode, encoding="utf-8") as handle:
                fd = -1
                yield handle, file_stat
        finally:
            if fd >= 0:
                os.close(fd)


def write_bound_text(path: Path, content: str) -> None:
    """Create or replace text through a bound parent descriptor."""
    with bound_parent(path, create_parents=True) as parent:
        try:
            fd = parent.open_file(os.O_WRONLY)
        except FileNotFoundError:
            fd = parent.open_file(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            _validate_regular_file(fd, path, writable=True)
            os.ftruncate(fd, 0)
            data = content.encode("utf-8")
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            os.close(fd)


def stage_bound_bytes(
    path: Path,
    data: bytes,
    *,
    leaf: str | None = None,
    mode: int = 0o600,
) -> Path:
    """Durably stage bytes beside ``path`` without following path components."""
    stage_leaf = leaf or f".corsia-write-{uuid4().hex}.stage"
    if Path(stage_leaf).name != stage_leaf:
        raise FilesystemBindingError("A staging filename cannot contain a path.")
    with bound_parent(path) as parent:
        fd = os.open(
            stage_leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            mode,
            dir_fd=parent.fd,
        )
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent.fd)
    return path.parent / stage_leaf


def bound_file_mode(path: Path) -> int:
    """Return the permission bits for one descriptor-bound regular file."""
    with bound_parent(path) as parent:
        fd = parent.open_file(os.O_RDONLY)
        try:
            file_stat = _validate_regular_file(fd, path, writable=False)
            return stat.S_IMODE(file_stat.st_mode)
        finally:
            os.close(fd)


def claim_bound_file(source: Path, claim: Path) -> int:
    """Atomically claim ``source`` and return the claimed file's mode.

    The journal-owned claim name is authoritative. Rename first, then validate
    the file through that name so a replacement at ``source`` cannot make the
    caller read bytes from a different inode than the one it claimed.
    """
    if source.parent != claim.parent or Path(claim.name).name != claim.name:
        raise FilesystemBindingError("A source claim must be a sibling file.")
    with bound_parent(source) as parent:
        try:
            os.stat(claim.name, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FilesystemBindingError(
                f"The durable source claim already exists: {claim}"
            )
        os.rename(
            parent.leaf,
            claim.name,
            src_dir_fd=parent.fd,
            dst_dir_fd=parent.fd,
        )
        os.fsync(parent.fd)
        claim_fd = os.open(
            claim.name,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
            dir_fd=parent.fd,
        )
        try:
            claim_stat = _validate_regular_file(claim_fd, claim, writable=True)
            return stat.S_IMODE(claim_stat.st_mode)
        finally:
            os.close(claim_fd)


def stage_bound_file(
    source: Path,
    path: Path,
    *,
    leaf: str | None = None,
    mode: int = 0o600,
) -> tuple[Path, str, int]:
    """Stream one bound file into a durable sibling stage.

    Returns the staged path, SHA-256 digest, and byte count. The source size is
    checked before and after the copy so a changing claimed file cannot be
    journaled as a complete promotion.
    """
    stage_leaf = leaf or f".corsia-write-{uuid4().hex}.stage"
    if Path(stage_leaf).name != stage_leaf:
        raise FilesystemBindingError("A staging filename cannot contain a path.")
    with bound_parent(source) as source_parent:
        source_fd = source_parent.open_file(os.O_RDONLY)
        try:
            source_stat = _validate_regular_file(source_fd, source, writable=False)
            with bound_parent(path) as destination_parent:
                stage_fd = os.open(
                    stage_leaf,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                    mode,
                    dir_fd=destination_parent.fd,
                )
                try:
                    os.fchmod(stage_fd, mode)
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := os.read(source_fd, 1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(stage_fd, view)
                            view = view[written:]
                    os.fsync(stage_fd)
                    final_source_stat = os.fstat(source_fd)
                    final_stage_stat = os.fstat(stage_fd)
                    if (
                        size != source_stat.st_size
                        or final_source_stat.st_size != source_stat.st_size
                        or final_stage_stat.st_size != size
                    ):
                        raise FilesystemBindingError(
                            f"Claimed file changed while it was staged: {source}"
                        )
                except BaseException:
                    os.close(stage_fd)
                    stage_fd = -1
                    try:
                        os.unlink(stage_leaf, dir_fd=destination_parent.fd)
                    finally:
                        os.fsync(destination_parent.fd)
                    raise
                finally:
                    if stage_fd >= 0:
                        os.close(stage_fd)
                os.fsync(destination_parent.fd)
                return path.parent / stage_leaf, digest.hexdigest(), size
        finally:
            os.close(source_fd)


def restore_claimed_file(claim: Path, destination: Path) -> bool:
    """Restore a claimed sibling entry without replacing a newer destination.

    Returns ``False`` when another entry already occupies ``destination``. A
    no-replace rename handles directories; a hard link makes the transition
    atomic for other entry types before the claim is removed. The entry is not
    opened: a non-regular replacement raced into the claimed pathname must be
    preserved rather than leaving the durable intent permanently wedged.
    """
    if claim.parent != destination.parent:
        raise FilesystemBindingError("A source claim must restore to its sibling.")
    with bound_parent(claim) as parent:
        claim_stat = os.stat(
            parent.leaf,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(claim_stat.st_mode):
            try:
                atomic_rename_noreplace_at(
                    parent.fd,
                    parent.leaf,
                    destination.name,
                )
            except FileExistsError:
                return False
            os.fsync(parent.fd)
            return True
        try:
            os.link(
                parent.leaf,
                destination.name,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            destination_stat = os.stat(
                destination.name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            if (
                destination_stat.st_dev != claim_stat.st_dev
                or destination_stat.st_ino != claim_stat.st_ino
            ):
                return False
        os.unlink(parent.leaf, dir_fd=parent.fd)
        os.fsync(parent.fd)
        return True


def replace_bound_file(source: Path, destination: Path) -> None:
    """Atomically replace ``destination`` with one bound regular ``source``."""
    with bound_parent(source) as source_parent:
        source_fd = source_parent.open_file(os.O_RDONLY)
        try:
            _validate_regular_file(source_fd, source, writable=False)
        finally:
            os.close(source_fd)
        with bound_parent(destination) as destination_parent:
            try:
                destination_fd = destination_parent.open_file(os.O_RDONLY)
            except FileNotFoundError:
                destination_fd = -1
            try:
                if destination_fd >= 0:
                    _validate_regular_file(
                        destination_fd,
                        destination,
                        writable=True,
                    )
            finally:
                if destination_fd >= 0:
                    os.close(destination_fd)
            os.replace(
                source_parent.leaf,
                destination_parent.leaf,
                src_dir_fd=source_parent.fd,
                dst_dir_fd=destination_parent.fd,
            )
            os.fsync(destination_parent.fd)
        if source.parent != destination.parent:
            os.fsync(source_parent.fd)


def unlink_bound_file(path: Path, *, missing_ok: bool = True) -> None:
    """Remove one bound regular file and persist the directory entry change."""
    with bound_parent(path) as parent:
        try:
            fd = parent.open_file(os.O_RDONLY)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        try:
            _validate_regular_file(fd, path, writable=True)
        finally:
            os.close(fd)
        os.unlink(parent.leaf, dir_fd=parent.fd)
        os.fsync(parent.fd)


def copy_bound_file(source: Path, destination: Path) -> None:
    """Copy one regular file between descriptor-bound paths."""
    with bound_parent(source) as source_parent:
        source_fd = source_parent.open_file(os.O_RDONLY)
        try:
            source_stat = _validate_regular_file(source_fd, source, writable=False)
            with bound_parent(destination, create_parents=True) as destination_parent:
                try:
                    destination_fd = destination_parent.open_file(os.O_WRONLY)
                except FileNotFoundError:
                    destination_fd = destination_parent.open_file(
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        mode=0o600,
                    )
                try:
                    _validate_regular_file(
                        destination_fd,
                        destination,
                        writable=True,
                    )
                    os.fchmod(destination_fd, stat.S_IMODE(source_stat.st_mode))
                    os.ftruncate(destination_fd, 0)
                    while chunk := os.read(source_fd, 1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            view = view[written:]
                    os.fsync(destination_fd)
                finally:
                    os.close(destination_fd)
                os.fsync(destination_parent.fd)
        finally:
            os.close(source_fd)


@contextmanager
def open_bound_directory(path: Path) -> Iterator[int]:
    """Open an authorized directory without following path components."""
    fd = _open_directory(path)
    try:
        yield fd
    finally:
        os.close(fd)


def list_bound_directory(path: Path) -> list[tuple[str, bool]]:
    """Return direct children and no-follow directory classification."""
    with open_bound_directory(path) as fd:
        entries: list[tuple[str, bool]] = []
        for name in os.listdir(fd):
            entry_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
            entries.append((name, stat.S_ISDIR(entry_stat.st_mode)))
        return entries


def bound_path_kind(path: Path) -> str:
    """Return ``file`` or ``directory`` without following the final component."""
    with bound_parent(path) as parent:
        try:
            entry_stat = os.stat(
                parent.leaf,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FilesystemBindingError(
                f"Authorized path changed before the filesystem effect: {path}"
            ) from exc
    if stat.S_ISREG(entry_stat.st_mode):
        return "file"
    if stat.S_ISDIR(entry_stat.st_mode):
        return "directory"
    raise FilesystemBindingError(f"Authorized path has unsupported type: {path}")


@dataclass(frozen=True)
class BoundTreeFile:
    """One regular file yielded while its parent descriptor remains open."""

    relative: Path
    file_stat: os.stat_result
    _parent_fd: int
    _name: str

    def read_text(self) -> str:
        try:
            fd = os.open(
                self._name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                dir_fd=self._parent_fd,
            )
        except OSError as exc:
            raise FilesystemBindingError(
                f"Search path changed before reading: {self.relative}"
            ) from exc
        try:
            current = _validate_regular_file(
                fd,
                self.relative,
                writable=False,
            )
            if (
                current.st_dev,
                current.st_ino,
            ) != (
                self.file_stat.st_dev,
                self.file_stat.st_ino,
            ):
                raise FilesystemBindingError(
                    f"Search path changed before reading: {self.relative}"
                )
            with os.fdopen(fd, "r", encoding="utf-8", errors="ignore") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd >= 0:
                os.close(fd)


def _walk_bound_directory(
    directory_fd: int,
    relative_root: Path,
    *,
    skip_directory: Callable[[Path], bool],
    refused: frozenset[Path],
    take_entry: Callable[[], bool] | None,
) -> Generator[BoundTreeFile, None, bool]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as listing:
            for entry in listing:
                if take_entry is not None and not take_entry():
                    return True
                names.append(entry.name)
    except OSError as exc:
        raise FilesystemBindingError(f"Search directory changed before traversal: {relative_root}") from exc

    for name in sorted(name for name in names if relative_root / name not in refused):
        relative = relative_root / name
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise FilesystemBindingError(
                f"Search path changed before traversal: {relative}"
            ) from exc

        if stat.S_ISDIR(entry_stat.st_mode):
            if skip_directory(relative):
                continue
            try:
                child_fd = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                raise FilesystemBindingError(
                    f"Search path changed before traversal: {relative}"
                ) from exc
            try:
                if (yield from _walk_bound_directory(
                    child_fd, relative, skip_directory=skip_directory, refused=refused, take_entry=take_entry,
                )):
                    return True
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            yield BoundTreeFile(
                relative=relative, file_stat=entry_stat, _parent_fd=directory_fd, _name=name,
            )
    return False


@contextmanager
def walk_bound_files(
    root: Path,
    *,
    skip_directory: Callable[[Path], bool] = frozenset[Path]().__contains__,
    take_entry: Callable[[], bool] | None = None,
) -> Iterator[Iterator[BoundTreeFile]]:
    """Yield regular files beneath a no-follow directory root, skipping any credential root (file
    or directory) and directories ``skip_directory`` refuses; a refused ``take_entry`` ends it."""
    with open_bound_directory(root) as root_fd:
        files = _walk_bound_directory(
            root_fd, Path(), take_entry=take_entry, skip_directory=skip_directory, refused=credential_paths_beneath(root))
        try:
            yield files
        finally:
            files.close()
