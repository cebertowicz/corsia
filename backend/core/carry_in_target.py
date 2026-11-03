"""Where a C1 carry-in case's lesson owner and target live, split from ``core.carry_in_facts``."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def owner_tray(db: sqlite3.Connection, owner: str, trays: dict[str, Path | None] | None = None) -> Path | None:
    """The owner's tray; ``trays`` memoizes it across one scan's lessons."""
    from core.placement_answer import thread_place

    if trays is not None and owner in trays:
        return trays[owner]
    tray = None
    if owner and db.execute("SELECT 1 FROM threads WHERE id=?", (owner,)).fetchone() is not None:
        tray = thread_place(db, owner).tray
    if trays is not None:
        trays[owner] = tray
    return tray


def canonical_target(tray: Path, workspace: Path, target: str, resolved_workspace: Path | None = None) -> str | None:
    """A case target as a workspace-relative path, the way placement receipts resolve one.

    The owner's scope root wins over the workspace root; a target outside the
    workspace has no canonical form. ``resolved_workspace`` spares a scan one
    resolve per lesson.
    """
    given = Path(target).expanduser()
    roots = [tray.parent.parent, workspace]
    candidates = [given] if given.is_absolute() else [root / given for root in roots]
    resolved_workspace = resolved_workspace or workspace.resolve()
    fallback = None
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_workspace):
            continue
        rel = resolved.relative_to(resolved_workspace).as_posix()
        if resolved.is_file():
            return rel
        fallback = fallback or rel
    return fallback
