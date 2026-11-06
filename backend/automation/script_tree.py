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
from pathlib import Path

from automation.script_schedules import MAX_SCRIPT_BYTES

_TOO_LARGE = ("the script's folder holds more than 1 MB; put the script and only what it "
              "loads in a folder of its own")


def read_tree(entry: Path) -> dict[str, bytes]:
    """Every file under ``entry``'s folder by relative path, with the bytes a run would load.

    A file link is read through to what it points at; a folder link is refused, because
    ``import pkg`` would load it while a walk that does not follow links skips it.
    """
    folder = entry.parent
    files: dict[str, bytes] = {}
    total = 0
    for root, dirs, names in os.walk(folder, followlinks=False):
        base = Path(root)
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in dirs:
            if (base / name).is_symlink():
                raise ValueError(f"{_relative(base / name, folder)} in the script's folder links to a "
                                 "folder; copy what the script loads into its own folder instead")
        for name in sorted(names):
            path = base / name
            if not path.is_symlink() and not path.is_file():
                continue  # a socket or pipe in the folder is not code a run can load
            if not path.is_file():
                raise ValueError(f"{_relative(path, folder)} in the script's folder is a broken link; "
                                 "remove it or point it at a file")
            total += path.stat().st_size
            if total > MAX_SCRIPT_BYTES:
                raise ValueError(_TOO_LARGE)
            files[_relative(path, folder)] = path.read_bytes()
    return files


def tree_sha256(files: dict[str, bytes]) -> str:
    """One canonical hash: sorted relative paths, each with its own sha256."""
    return digest({name: hashlib.sha256(data).hexdigest() for name, data in files.items()})


def digest(hashes: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(
        hashes, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def approved_tree_sha256(authentication: dict, entry: Path, script_sha256: str | None) -> str:
    """The tree hash a run must match.

    An approval made before folders were reviewed carries none; it reviewed the entry
    alone, so it still runs only while the folder holds nothing but that same entry.
    """
    return authentication.get("tree_sha256") or digest({entry.name: script_sha256 or ""})


def _relative(path: Path, folder: Path) -> str:
    return path.relative_to(folder).as_posix()
