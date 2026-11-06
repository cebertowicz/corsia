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


def compare_bytes_for_save(path: Path, *, guidance: bool) -> bytes | None:
    """The expectation a guidance save compares its destination against: the file's own bytes.

    Read them before any text read, so a change between the two reads comes back stale instead of quietly
    overwritten: a text read turns CRLF and a lone CR into LF, and that text is not what the save will
    compare. A write that records no history carries no expectation and reads nothing extra.
    """
    if not guidance:
        return None
    with open_bound_binary(path) as (handle, _stat):
        return handle.read()
