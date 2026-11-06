"""What an authenticated script's approval covers: every file in the script's folder.

``python3 <entry>`` imports siblings from the entry's folder, and bash or node load
them by relative path, so the approved bytes are the folder's, not only the entry's.
Only directories named exactly ``__pycache__`` and files named ``.DS_Store`` are left out.
Authenticated Python runs keep their bytecode elsewhere (``script_sandbox.script_command``),
so none is read there; Finder writes ``.DS_Store`` when the folder is merely opened, and
no interpreter loads it. Anything else the walk can't read as a regular file refuses the
folder, so the hash covers everything a run can load from it.
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
from tools.filesystem_effects import FilesystemBindingError, open_bound_directory

_NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

_TOO_LARGE = ("the script's folder holds more than 1 MB; put the script and only what it "
              "loads in a folder of its own")


def read_tree(entry: Path, denied: Callable[[Path], str | None] | None = None) -> dict[str, bytes]:
    """Every file under ``entry``'s folder by relative path, with the bytes a run would load.

    Every refusal is a ``ValueError`` naming the entry and its fix. A link is refused: a run
    would load what it points at, which may lie outside what the run, or the agent asking
    for review, can read. ``denied`` is the requester's read check; the folder and
    everything in it pass it before anything is read. Every read goes through descriptors
    opened without following links, so a path swapped for a link after its check fails to
    open instead of being read.
    """
    folder = entry.parent
    files: dict[str, bytes] = {}
    _check(folder, denied)
    try:
        with open_bound_directory(folder) as folder_fd:
            _read_folder(folder_fd, folder, Path(), denied, files)
    except FilesystemBindingError as exc:
        raise ValueError(f"{folder} can't be opened without going through a link; schedule the "
                         "script by its real path") from exc
    return files


def _read_folder(dir_fd: int, folder: Path, relative: Path, denied: Callable[[Path], str | None] | None,
                 files: dict[str, bytes]) -> None:
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        raise ValueError(_unreadable(relative, exc)) from exc
    for name in names:
        path = relative / name
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        except OSError as exc:
            raise ValueError(_unreadable(path, exc)) from exc
        if (stat.S_ISDIR(mode) and name == "__pycache__") or (stat.S_ISREG(mode) and name == ".DS_Store"):
            continue
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(_special(path))
        _check(folder / path, denied)
        try:
            fd = os.open(name, _NO_FOLLOW | (os.O_DIRECTORY if stat.S_ISDIR(mode) else os.O_NONBLOCK),
                         dir_fd=dir_fd)
        except OSError as exc:
            raise ValueError(_special(path) if exc.errno == errno.ELOOP else _unreadable(path, exc)) from exc
        try:
            if stat.S_ISDIR(mode):
                _read_folder(fd, folder, path, denied, files)
                continue
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(_special(path))
            room = MAX_SCRIPT_BYTES - sum(map(len, files.values()))
            data = os.read(fd, room + 1)
            if len(data) > room:
                raise ValueError(_TOO_LARGE)
            files[path.as_posix()] = data
        except OSError as exc:
            raise ValueError(_unreadable(path, exc)) from exc
        finally:
            os.close(fd)


def _special(path: Path) -> str:
    return (f"{path.as_posix()} in the script's folder is a link or another kind of special file; "
            "copy what the script loads into its own folder instead")


def _unreadable(path: Path, exc: OSError) -> str:
    where = f"{path.as_posix()} in the script's folder" if path.parts else "the script's folder"
    if isinstance(exc, FileNotFoundError):
        return f"{where} went away while the folder was being read"
    return f"{where} can't be read; make it readable, or move it out if the script doesn't load it"


def tree_sha256(files: dict[str, bytes]) -> str:
    """One canonical hash: sorted relative paths, each with its own sha256."""
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return hashlib.sha256(json.dumps(
        hashes, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _check(path: Path, denied: Callable[[Path], str | None] | None) -> None:
    if denied is not None and (denial := denied(path)):
        raise ValueError(denial)
