"""Compare-and-swap helpers shared by guidance history's text and binary paths."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from infra.guidance_history_paths import digest
from infra.guidance_messages import SAVE_STALE
from infra.guidance_save_types import SaveResult


def stale_path(
    expected: dict[Path, str | None] | None,
    *,
    read_bytes: Callable[[Path], bytes | None],
) -> Path | None:
    """Return the first destination whose current content differs from its expectation.

    Every expectation is :func:`digest` of the destination's own bytes, so this reads bytes too: a
    text read turns CRLF and a lone CR into LF and would refuse a file nobody changed."""
    for path, want in (expected or {}).items():
        try:
            current = digest(read_bytes(path))
        except OSError:
            current = ""
        if current != want:
            return path
    return None


def stale_result(
    expected: dict[Path, str | None] | None, *,
    read_bytes: Callable[[Path], bytes | None],
) -> SaveResult | None:
    """Return a stale result when the destination changed after the caller read it."""
    path = stale_path(expected, read_bytes=read_bytes)
    return SaveResult("stale", "deferred-destination-changed", line=SAVE_STALE.format(path=path)) if path else None
