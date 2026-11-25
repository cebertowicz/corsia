"""Open log files without following a final symlink."""

import hashlib
import os
from pathlib import Path
import sys

from infra.platform_paths import APP_FOLDER


def logs_dir(state_root: Path, *, home: Path | None = None, platform: str | None = None) -> Path:
    """Resolve the instance's log folder without changing existing logs."""
    if (sys.platform if platform is None else platform) != "darwin":
        return state_root / "logs"
    name = hashlib.sha256(os.fsencode(state_root.resolve())).hexdigest()[:12]
    return (Path.home() if home is None else home) / "Library" / "Logs" / APP_FOLDER / name


def nofollow_log_opener(path: str, flags: int) -> int:
    """Use as an opener for Python's open() on CORSIA-owned logs."""
    return os.open(path, flags | os.O_NOFOLLOW, 0o666)
