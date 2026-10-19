"""Whether the local model's download is finished, and whether its volume has room for the rest."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


class DiskSpaceError(RuntimeError):
    """The models volume lacks room for the download; carry the exact deficit."""

    def __init__(self, message: str, *, bytes_to_free: int) -> None:
        super().__init__(message)
        self.bytes_to_free = bytes_to_free


def _present_bytes(folder: Path) -> int:
    """Bytes already under ``folder`` (finished and partial files); 0 when it is absent."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def download_complete(folder: Path, download_bytes: int) -> bool:
    """Whether ``folder`` holds a finished download: every expected byte, and no partial file.

    A cancelled or failed download leaves its folder behind (oMLX resumes there), so the
    folder existing proves nothing. Partial files carry Hugging Face's ``.incomplete``
    suffix until they are renamed into place.
    """
    if not folder.is_dir():
        return False
    for _root, _dirs, files in os.walk(folder):
        if any(name.endswith(".incomplete") for name in files):
            return False
    return _present_bytes(folder) >= download_bytes


def download_disk_space(download_bytes: int, model_dir: Path | None, repository: str) -> dict[str, int | str]:
    """Describe the actual remaining download space requirement without guessing.

    oMLX resumes inside ``<model_dir>/<repository>``, so present partial bytes are
    credited and the existing 10% safety margin applies only to the remainder.
    """
    if model_dir is None:
        return {"state": "unknown"}
    path = model_dir
    try:
        while not path.exists() and path != path.parent:
            path = path.parent
        free = shutil.disk_usage(path).free
        remaining = max(0, download_bytes - _present_bytes(model_dir / repository))
    except OSError:
        return {"state": "unknown"}
    required = int(remaining * 1.1)
    return {
        "state": "ready" if free >= required else "insufficient",
        "free_bytes": free,
        "remaining_bytes": remaining,
        "required_bytes": required,
    }


def require_free_disk(download_bytes: int, model_dir: Path | None, repository: str) -> None:
    """Raise DiskSpaceError when oMLX's download volume lacks room for the remainder."""
    space = download_disk_space(download_bytes, model_dir, repository)
    if space["state"] == "unknown":
        log.warning("Free disk space check skipped: oMLX download directory is unavailable")
        return
    free = int(space["free_bytes"])
    needed = int(space["required_bytes"])
    if space["state"] == "insufficient":
        short = (needed - free) / 1000**3
        raise DiskSpaceError(
            f"This download needs {needed / 1000**3:.0f} GB of free disk space and this computer has "
            f"{free / 1000**3:.0f} GB. Free up about {short:.0f} GB, then run local setup again.",
            bytes_to_free=max(1, needed - free),
        )
