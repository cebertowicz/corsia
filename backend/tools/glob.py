"""Fast file pattern matching tool."""

import asyncio
from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.filesystem_effects import match_relative_glob, walk_bound_files
from tools.filesystem_gate import _resolve_path, _resolve_path_for_action
from tools.glob_result import other_thread_result, render_glob_result
from tools.glob_walk import Traversal, select_matches
from tools.search_scope import SearchScope, keep_path, pattern_components, scoped_match, search_identity


def _glob_scope(search_dir: Path, current_thread_id: str | None, all_threads: bool) -> SearchScope:
    """Glob's scope: the shared rules, and ``.env`` files are listed."""
    return SearchScope(current_thread_id, all_threads, root_name=search_dir.name, lists_env=True)


class GlobTool(Tool):
    """Fast file pattern matching — find files by name patterns."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Fast file pattern matching tool that finds files by name patterns.\n"
            "Supports glob patterns like '**/*.py', 'src/**/*.ts', '*.md'.\n"
            "Returns matching file paths sorted by modification time (newest first).\n"
            "Skips hidden files and folders (except .env and your own thread's .threads "
            "folder), node_modules, __pycache__ and venv unless the pattern names them "
            "(for example '.github/workflows/*.yml' or '.worktrees/*/src/**/*.py'); "
            "passing one as path also searches inside it.\n\n"
            "USE THIS when you need to:\n"
            "- Find files by name or extension (e.g., all Python files)\n"
            "- Locate a specific file when you know part of its name\n"
            "- Discover project structure (e.g., '**/*.py' to see all Python files)\n"
            "- Find config files (e.g., '**/config.*', '**/*.json')\n\n"
            "DO NOT use this when:\n"
            "- Searching for content inside files (use grep instead)\n"
            "- You already know the exact file path (use read_file instead)\n"
            "- Listing a single directory's contents (use list_dir instead)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files against (e.g., '**/*.py', 'src/**/*.ts', '*.md')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in. Defaults to workspace root if not specified.",
                },
                "all_threads": {
                    "type": "boolean",
                    "description": "Also search other threads' .threads folders. Default: false — search covers shared files and your own thread's folder.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, pattern: str, path: str | None = None, all_threads: bool = False, **kwargs: Any,
    ) -> str:
        try:
            current_thread_id = kwargs.get("_thread_id")
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={
                    "pattern": pattern,
                    "path": path or ".",
                    "all_threads": all_threads,
                },
                path=path or ".",
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=current_thread_id,
                project_id=kwargs.get("_thread_project_id"),
            )
            search_dir = authorized.resolved
            current_thread_id, all_threads = search_identity(search_dir, current_thread_id, all_threads)
            if not keep_path(search_dir, current_thread_id, all_threads):
                return other_thread_result(pattern, search_dir)

            if authorized.reviewed:
                return await asyncio.to_thread(
                    self._glob_reviewed,
                    pattern,
                    search_dir,
                    current_thread_id,
                    all_threads,
                )

            return await asyncio.to_thread(
                self._glob_unreviewed,
                pattern,
                search_dir,
                path,
                current_thread_id,
                all_threads,
            )

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {str(e)}"

    def _glob_unreviewed(
        self,
        pattern: str,
        search_dir: Path,
        requested_path: str | None,
        current_thread_id: str | None,
        all_threads: bool,
    ) -> str:
        """Traverse an authorized ordinary path outside the server event loop."""
        path = requested_path
        try:
            if not search_dir.exists():
                return f"Error: Directory not found: {path or str(self._workspace)}"
            if not search_dir.is_dir():
                return f"Error: Not a directory: {path or str(self._workspace)}"

            scope = _glob_scope(search_dir, current_thread_id, all_threads)
            traversal = Traversal(scope)
            matches: list[tuple[str, Path]] = []
            # The walk never selects a path outside the search scope.
            for relative in select_matches(search_dir, pattern, traversal):
                match = search_dir.joinpath(*relative)
                resolved_match = match.resolve(strict=False)
                if not resolved_match.is_relative_to(search_dir):
                    try:
                        resolved_match = _resolve_path(
                            str(match), self._workspace, self._allowed_dir
                        )
                    except PermissionError:
                        continue
                if resolved_match.is_file():
                    # A link may resolve into another thread's folder.
                    if not keep_path(
                        resolved_match, scope.current_thread_id, scope.all_threads
                    ):
                        continue
                    matches.append((str(match.relative_to(search_dir)), resolved_match))

            matches.sort(key=lambda item: item[1].stat().st_mtime, reverse=True)
            newest_first = [display for display, _resolved in matches]
            return render_glob_result(pattern, search_dir, newest_first, traversal)
        except PermissionError as e:
            return f"Error: {e}"

    @staticmethod
    def _glob_reviewed(
        pattern: str,
        search_dir: Path,
        current_thread_id: str | None,
        all_threads: bool,
    ) -> str:
        scope = _glob_scope(search_dir, current_thread_id, all_threads)
        traversal = Traversal(scope, components=pattern_components(pattern))
        matches: list[tuple[Path, float]] = []
        with walk_bound_files(
            search_dir,
            skip_directory=traversal.prunes_directory,
            take_entry=traversal.take,
        ) as files:
            for entry in files:
                relative = entry.relative
                if not match_relative_glob(relative, pattern):
                    continue
                if not scoped_match(relative.parts, pattern, scope):
                    parent = relative.parent.name or scope.root_name
                    traversal.note_skipped(parent, relative.name)
                    continue
                matches.append((relative, entry.file_stat.st_mtime))

        matches.sort(key=lambda item: item[1], reverse=True)
        newest_first = [str(relative) for relative, _mtime in matches]
        return render_glob_result(pattern, search_dir, newest_first, traversal)
