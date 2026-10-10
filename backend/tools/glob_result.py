"""Agent-facing text for glob results: truncation, skipped folders and early stops."""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath

from tools.glob_walk import Traversal
from tools.search_scope import THREADS_NOTICE

LIST_CAP = 100
FOLDER_LINES = 10
SKIPPED_NOTICE = (
    "Not searched: hidden files and folders (except .env and your own thread's "
    ".threads folder), node_modules, __pycache__ and venv. Name one in the "
    "pattern (for example .github/**/*.yml), or pass it as path, to search it."
)


def render_glob_result(
    pattern: str,
    search_dir: Path,
    newest_first: list[str],
    traversal: Traversal,
) -> str:
    """Render matches newest first, declaring truncation, skips and an early stop.

    A complete, untruncated, non-empty result is exactly the listing; the extra
    lines appear only when the listing is empty, truncated or incomplete.
    """
    if newest_first:
        result = "\n".join(newest_first[:LIST_CAP])
    else:
        result = f"No files matching '{pattern}' in {search_dir}"
    truncated = len(newest_first) > LIST_CAP
    if truncated:
        result += f"\n\n... and {len(newest_first) - LIST_CAP} more files"
        result += "\n" + folder_breakdown(newest_first)
    if truncated or not newest_first or traversal.stopped:
        if traversal.skipped:
            result += "\n" + SKIPPED_NOTICE
        if traversal.skipped_threads:
            result += "\n" + THREADS_NOTICE
    if traversal.stopped:
        return (
            f"INCOMPLETE: stopped after examining {traversal.examined} entries; "
            f"results are partial — narrow path or pattern\n\n{result}"
        )
    return result


def other_thread_result(pattern: str, search_dir: Path) -> str:
    """The answer for a search started inside another thread's folder."""
    return f"No files matching '{pattern}' in {search_dir}\n{THREADS_NOTICE}"


def folder_breakdown(paths: list[str]) -> str:
    """Count matches per folder one level below the folder all matches share."""
    folders = [PurePosixPath(path).parent.parts for path in paths]
    shared = _shared_prefix(folders)
    counts = Counter(folder[: len(shared) + 1] for folder in folders)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = ["Matches by folder:"]
    for folder, count in ranked[:FOLDER_LINES]:
        label = "/".join(folder) + "/" if folder else "./"
        where = " directly inside" if len(folder) == len(shared) else ""
        lines.append(f"- {label} ({count}{where})")
    if len(ranked) > FOLDER_LINES:
        lines.append(f"- (+{len(ranked) - FOLDER_LINES} more folders)")
    return "\n".join(lines)


def _shared_prefix(folders: list[tuple[str, ...]]) -> tuple[str, ...]:
    shared = folders[0]
    for folder in folders[1:]:
        length = 0
        while length < min(len(shared), len(folder)) and shared[length] == folder[length]:
            length += 1
        shared = shared[:length]
    return shared
