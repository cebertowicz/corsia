"""Read an exact recorded guidance blob, never substitute a current file."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.lesson_report_inventory import source_record


def recorded_source(
    reader: Callable[[object, object], tuple[bytes | None, str | None]] | None,
    receipt: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse bytes supplied by the one public, read-only Git owner."""
    if reader is None:
        return None, "workspace guidance history unavailable"
    data, reason = reader(receipt.get("git_blob_oid"), receipt.get("raw_sha256"))
    if data is None:
        return None, reason
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "exact recorded file object unreadable"
    source = source_record(text, str(receipt["path"]))
    if source is None:
        return None, "recorded file is not a validated lesson or capture"
    return source | {"git_blob_oid": receipt["git_blob_oid"]}, None
