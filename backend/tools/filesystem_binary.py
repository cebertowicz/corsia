"""Descriptor-bound binary filesystem reads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
from typing import IO

from tools.filesystem_effects import _validate_regular_file, bound_parent


@contextmanager
def open_bound_binary(path: Path) -> Iterator[tuple[IO[bytes], os.stat_result]]:
    """Open one regular file for byte reads through a bound parent descriptor."""
    with bound_parent(path) as parent:
        fd = parent.open_file(os.O_RDONLY)
        try:
            file_stat = _validate_regular_file(fd, path, writable=False)
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                yield handle, file_stat
        finally:
            if fd >= 0:
                os.close(fd)
