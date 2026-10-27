"""The Python grep walk's view of a C1 eval thread's projected target.

``core.context_projection`` decides which file a thread sees at a recorded
revision; these two helpers apply that decision to the walk's file list and to
each file it reads, so the projected target is searched at its tested bytes.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

MAX_SEARCH_FILE_BYTES = 10 * 1024 * 1024


def read_search_text(resolved_path: Path, projection=None) -> str | None:
    """A searched file's text: the projected bytes for a projected target, else the live file."""
    if projection is not None and projection.covers(resolved_path):
        return projection.data.decode("utf-8")
    try:
        if resolved_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
            return None
        return resolved_path.read_text(encoding="utf-8", errors="ignore")
    except (PermissionError, OSError):
        return None


def projected_files(
    walked: Iterable[tuple[Path, Path]], search_dir: Path, glob_filter: str | None, projection,
) -> list[tuple[Path, Path]]:
    """The walk's files with a projected target in place of its live file (absent there: dropped)."""
    files = [
        (fpath, resolved) for fpath, resolved in walked
        if projection is None or not projection.covers(resolved)
    ]
    if projection is not None and projection.data is not None:
        # Present at the tested revision even if absent (or filtered) live.
        target = projection.path
        if target == search_dir or glob_filter is None or target.match(glob_filter):
            files.append((target, target))
    return files
