"""Small filesystem durability primitives shared by atomic writers."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes before an atomic writer returns."""
    if sys.platform.startswith("win"):
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
