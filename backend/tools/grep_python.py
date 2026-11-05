"""File selection for grep's Python fallback."""

from collections.abc import Iterator
from pathlib import Path

from tools.filesystem_gate import resolve_path
from tools.glob_walk import select_grep_files
from tools.search_scope import SearchScope, keep_path
from tools.search_fence import SearchFence


def iter_python_files(
    search_dir: Path, glob_filter: str | None,
    current_thread_id: str | None, all_threads: bool, fence: SearchFence | None = None,
    *, workspace: Path | None, allowed_dir: Path | None, project_fence=None,
) -> Iterator[tuple[Path, Path]]:
    """Yield display paths with authorized resolved targets for fallback reads."""
    scope = SearchScope(current_thread_id, all_threads, root_name=search_dir.name, fence=fence)
    if search_dir.is_file():
        candidates = iter((search_dir,))
    else:
        candidates = (
            search_dir.joinpath(*relative)
            for relative in select_grep_files(search_dir, glob_filter, scope)
        )
    for candidate in candidates:
        if fence and fence.excludes(candidate):
            continue
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(search_dir):
            try:
                resolved = resolve_path(
                    str(candidate), workspace, allowed_dir,
                    thread_id=fence.thread_id if fence else current_thread_id,
                    fence=project_fence,
                )
            except PermissionError:
                continue
        if resolved.is_file() and keep_path(resolved, current_thread_id, all_threads):
            yield candidate, resolved
