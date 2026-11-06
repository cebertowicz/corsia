"""What an authenticated script's approval covers: every file in the script's folder.

``python3 <entry>`` imports siblings from the entry's folder, and bash or node load
them by relative path, so the approved bytes are the folder's, not only the entry's.
Only directories named exactly ``__pycache__`` are left out; authenticated Python runs
keep their bytecode elsewhere (``script_sandbox.script_command``), so none is read there.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from automation.script_schedules import MAX_SCRIPT_BYTES

_TOO_LARGE = ("the script's folder holds more than 1 MB; put the script and only what it "
              "loads in a folder of its own")


def read_tree(entry: Path, denied: Callable[[Path], str | None] | None = None) -> dict[str, bytes]:
    """Every file under ``entry``'s folder by relative path, with the bytes a run would load.

    A link is refused: a run would load what it points at, which may lie outside what
    the run, or the agent asking for review, can read. ``denied`` is the requester's
    read check; the folder and everything in it pass it before anything is read.
    """
    folder = entry.parent
    files: dict[str, bytes] = {}
    total = 0
    _check(folder, denied)
    for root, dirs, names in os.walk(folder, followlinks=False):
        base = Path(root)
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in [*dirs, *sorted(names)]:
            if (base / name).is_symlink():
                raise ValueError(f"{_relative(base / name, folder)} in the script's folder is a link; "
                                 "copy what the script loads into its own folder instead")
            _check(base / name, denied)
        for name in sorted(names):
            path = base / name
            if not path.is_file():
                continue  # a socket or pipe in the folder is not code a run can load
            total += path.stat().st_size
            if total > MAX_SCRIPT_BYTES:
                raise ValueError(_TOO_LARGE)
            files[_relative(path, folder)] = path.read_bytes()
    return files


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
