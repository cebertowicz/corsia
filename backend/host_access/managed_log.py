"""Crash-safe bounded file mechanics for managed-process logs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

_READ_MARGIN_BYTES = 256


def tail_log_path(log_path: Path) -> Path:
    return log_path.with_name(f"{log_path.stem}.tail.log")


def read_log_tail(log_path: Path, *, long_running: bool, limit_bytes: int) -> bytes:
    path = tail_log_path(log_path) if long_running else log_path
    if long_running and not path.exists():
        path = log_path
    try:
        with path.open("rb") as log_file:
            size = log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, size - limit_bytes - _READ_MARGIN_BYTES))
            return log_file.read()
    except FileNotFoundError:
        return b""


def open_new_log(path: Path) -> IO[bytes]:
    """Create one private log without replacing an existing owned path."""
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")


def retain_tail(path: Path, keep_bytes: int) -> int:
    """Retain at most the final bytes in the same durably indexed path."""
    with path.open("r+b") as current:
        size = current.seek(0, os.SEEK_END)
        current.seek(max(0, size - keep_bytes))
        kept = current.read()
        current.seek(0)
        current.write(kept)
        current.truncate()
    return len(kept)
