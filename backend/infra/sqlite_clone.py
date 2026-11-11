"""Copy-on-write snapshot of a SQLite database file whose write lock the caller holds.

The pre-migration backup used to copy every page through SQLite's backup API: about 0.5 GB/s, so close to a
minute on a 25 GB database, all of it inside the migration's write hold while the app cannot start. On a
file system that clones (APFS on macOS; btrfs or XFS on Linux) the same snapshot is a metadata operation
that takes milliseconds and shares blocks with the live database until either side changes a page.

Why a clone is a consistent snapshot here: the caller holds the database's write lock (``BEGIN IMMEDIATE``)
and has written nothing yet. No other connection can commit, so the WAL can only grow by nothing, and only a
writer can reset it, so it cannot be reset either. A concurrent passive checkpoint may copy WAL pages into
the main file between the two clones, but the cloned WAL still holds those frames and replays the same
pages over them. The pair is therefore exactly what SQLite recovers after a power cut at that instant,
which it is built to recover. The clone is then opened once to fold its WAL in, so the backup is one
self-contained file like the backup API's.

Anything that is not a clone-capable file system raises ``CloneUnsupported`` before a target file exists,
and the caller falls back to the backup API.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sqlite3
import sys
from pathlib import Path

#: Linux ``FICLONE`` ioctl request number (``_IOW(0x94, 9, int)``).
_FICLONE = 0x40049409

#: errno values that mean "this file system or platform cannot clone", never a real failure.
_UNSUPPORTED = {
    errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOSYS, errno.ENOTTY, errno.EPERM,
}


class CloneUnsupported(Exception):
    """The platform or file system cannot clone; no target file was left behind."""


def _clone_file(source: Path, target: Path) -> None:
    """Clone one file to a new path, owner-only. Raises ``CloneUnsupported`` or ``OSError``."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        clonefile = getattr(libc, "clonefile", None)
        if clonefile is None:
            raise CloneUnsupported("clonefile is not available")
        if clonefile(os.fsencode(source), os.fsencode(target), 0) != 0:
            code = ctypes.get_errno()
            if code in _UNSUPPORTED:
                raise CloneUnsupported(os.strerror(code))
            raise OSError(code, os.strerror(code), str(target))
        # clonefile copies the live file's mode, so until this narrows it the clone is readable by
        # exactly whoever can already read the live database beside it, and no one else.
        try:
            os.chmod(target, 0o600)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return
    if not sys.platform.startswith("linux"):
        raise CloneUnsupported(f"no clone on {sys.platform}")
    import fcntl

    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(source, "rb") as reader:
            fcntl.ioctl(descriptor, _FICLONE, reader.fileno())
    except OSError as error:
        os.close(descriptor)
        target.unlink(missing_ok=True)
        if error.errno in _UNSUPPORTED:
            raise CloneUnsupported(os.strerror(error.errno)) from error
        raise
    os.close(descriptor)


def _flush(path: Path) -> None:
    """Push the file's written pages down before cloning, whichever process wrote them."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def clone_database(source: Path, target: Path, *, expected_user_version: int) -> None:
    """Snapshot ``source`` (and its WAL) into the new self-contained file ``target``.

    The caller must hold ``source``'s write lock and must not have written under it. Raises
    ``CloneUnsupported`` when cloning is not possible, leaving no target; any other failure removes
    what it wrote and re-raises.
    """
    wal = source.with_name(f"{source.name}-wal")
    has_wal = wal.exists() and wal.stat().st_size > 0
    _flush(source)
    if has_wal:
        _flush(wal)
    _clone_file(source, target)
    target_wal = target.with_name(f"{target.name}-wal")
    try:
        if has_wal:
            _clone_file(wal, target_wal)
        snapshot = sqlite3.connect(target)
        try:
            version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
            if version != expected_user_version:
                raise RuntimeError(
                    f"cloned database reads user_version {version}, expected {expected_user_version}"
                )
            if has_wal:
                busy, _frames, _moved = snapshot.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise RuntimeError("could not fold the cloned WAL into the backup")
        finally:
            snapshot.close()
    except BaseException:
        for leftover in (target, target_wal, target.with_name(f"{target.name}-shm")):
            leftover.unlink(missing_ok=True)
        raise
    for leftover in (target_wal, target.with_name(f"{target.name}-shm")):
        leftover.unlink(missing_ok=True)
