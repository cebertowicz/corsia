"""Which retained context files were edited after they were installed.

A source edit reaches a running conversation only at the next checkpoint
install, on every harness (``core.installed_context``). The preview therefore
compares each retained file-backed row's recorded ``raw_sha256`` with the file
on disk now, and the context sheet shows the result. Display only: the
snapshot hash, the delivered bytes and the guidance manifest never see it.

Only a retained row whose recorded version has status ``read`` can be
compared. Everything else (a row read fresh in this preview, a recorded
``changed_since_read``/``unreadable``, a row installed before versions were
recorded) is unknown, and unknown carries no flag.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from core.effective_context import ContextSourceEntry
from core.installed_context import ADOPTION_RETAINED

EDITED_SINCE_INSTALL = "edited_since_install"


def _disk_sha256(path: str) -> str | None:
    """Hash the file once; None when it is unreadable or changed during the read."""
    try:
        target = Path(path)
        before = target.stat()
        data = target.read_bytes()
        after = target.stat()
    except OSError:
        return None
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        return None
    return hashlib.sha256(data).hexdigest()


def edited_since_install(source: ContextSourceEntry) -> bool | None:
    """True or False when the row is comparable, None when unknown."""
    path = (source.metadata or {}).get("path")
    raw = source.raw_version
    if (
        not isinstance(path, str) or not path
        or source.inclusion not in {"included", "summarized"}
        or not raw or raw.get("status") != "read"
        or not isinstance(raw.get("raw_sha256"), str)
    ):
        return None
    current = _disk_sha256(path)
    return None if current is None else current != raw["raw_sha256"]


def annotate_pending_edits(usage: dict[str, Any], collection: Any) -> dict[str, Any]:
    """Add ``edited_since_install`` to the preview's retained file-backed rows."""
    installed = getattr(collection, "installed", None)
    if installed is None or installed.adoption != ADOPTION_RETAINED:
        return usage
    flags: dict[str, bool] = {}
    for source in collection.sources:
        flag = edited_since_install(source)
        if flag is not None:
            flags[source.source_id] = flag
    snapshot = usage.get("context_snapshot")
    for row in (snapshot or {}).get("source_inventory") or ():
        if row.get("source_id") in flags:
            row[EDITED_SINCE_INSTALL] = flags[row["source_id"]]
    return usage
