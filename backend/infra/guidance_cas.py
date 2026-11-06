"""Compare-and-swap helpers shared by guidance history's text and binary paths."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from infra.guidance_history_paths import digest
from infra.guidance_messages import SAVE_STALE
from infra.guidance_save_types import SaveResult


def stale_path(
    expected: dict[Path, str | None] | None,
    *,
    binary_safe_history: bool,
    read_bytes: Callable[[Path], bytes | None],
) -> Path | None:
    """Return the first destination whose current content differs from its expectation."""
    for path, want in (expected or {}).items():
        try:
            if binary_safe_history:
                content = read_bytes(path)
                current = hashlib.sha256(content).hexdigest() if content is not None else None
            else:
                current = digest(path.read_text()) if path.exists() else None
        except (OSError, UnicodeDecodeError):
            current = ""
        if current != want:
            return path
    return None


def stale_result(
    expected: dict[Path, str | None] | None, *, binary_safe_history: bool,
    read_bytes: Callable[[Path], bytes | None],
) -> SaveResult | None:
    """Return a stale result when the destination changed after the caller read it."""
    path = stale_path(expected, binary_safe_history=binary_safe_history, read_bytes=read_bytes)
    return SaveResult("stale", "deferred-destination-changed", line=SAVE_STALE.format(path=path)) if path else None
