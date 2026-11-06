"""What an authenticated script's approval covers: every file in the script's folder.

``python3 <entry>`` imports siblings from the entry's folder, and bash or node load
them by relative path, so the approved bytes are the folder's, not only the entry's.
Only directories named exactly ``__pycache__`` and files named ``.DS_Store`` are left out.
Authenticated Python runs keep their bytecode elsewhere (``script_sandbox.script_command``),
so none is read there; Finder writes ``.DS_Store`` when the folder is merely opened, and
no interpreter loads it.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

from automation.script_schedules import MAX_SCRIPT_BYTES

_TOO_LARGE = ("the script's folder holds more than 1 MB; put the script and only what it "
              "loads in a folder of its own")


def read_tree(entry: Path, denied: Callable[[Path], str | None] | None = None) -> dict[str, bytes]:
    """Every file under ``entry``'s folder by relative path, with the bytes a run would load.

    A link is refused: a run would load what it points at, which may lie outside what
    the run, or the agent asking for review, can read. ``denied`` is the requester's
    read check; the folder and everything in it pass it before anything is read. Every
    read goes through descriptors opened without following links, so a path swapped for
    a link after its check fails to open instead of being read.
    """
    folder = entry.parent
    files: dict[str, bytes] = {}
    total = 0
    _check(folder, denied)
    folder_fd = _open_folder(folder)
    try:
        for root, dirs, names, dir_fd in os.fwalk(".", dir_fd=folder_fd, follow_symlinks=False):
            base = folder / root
            dirs[:] = sorted(name for name in dirs if name != "__pycache__")
            names = sorted(name for name in names if name != ".DS_Store")
            for name in [*dirs, *names]:
                if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                    raise ValueError(_link(base / name, folder))
                _check(base / name, denied)
            for name in names:
                data = _read_file(name, dir_fd, base / name, folder, MAX_SCRIPT_BYTES - total)
                if data is None:
                    continue  # a socket or pipe in the folder is not code a run can load
                total += len(data)
                if total > MAX_SCRIPT_BYTES:
                    raise ValueError(_TOO_LARGE)
                files[_relative(base / name, folder)] = data
    finally:
        os.close(folder_fd)
    return files


def _open_folder(folder: Path) -> int:
    """``folder`` opened one component at a time from the root, none of them a link."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in folder.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(f"{folder} goes through a link; schedule the script by its "
                                     "real path") from exc
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(name: str, dir_fd: int, path: Path, folder: Path, room: int) -> bytes | None:
    """The regular file ``name`` under ``dir_fd``, never through a link; None when it isn't one."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(_link(path, folder)) from exc
        raise
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        return stream.read(max(room, 0) + 1)


def _link(path: Path, folder: Path) -> str:
    return (f"{_relative(path, folder)} in the script's folder is a link; "
            "copy what the script loads into its own folder instead")


def tree_sha256(files: dict[str, bytes]) -> str:
    """One canonical hash: sorted relative paths, each with its own sha256."""
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return hashlib.sha256(json.dumps(
        hashes, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _check(path: Path, denied: Callable[[Path], str | None] | None) -> None:
    if denied is not None and (denial := denied(path)):
        raise ValueError(denial)


def _relative(path: Path, folder: Path) -> str:
    return path.relative_to(folder).as_posix()
