"""Content search tool — search inside files using regex."""

import asyncio
import re
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from infra.constants import GREP_OUTPUT_CHAR_CAP
from tools.base import Tool
from tools.filesystem_effects import bound_path_kind, open_bound_text, walk_bound_files
from tools.filesystem_gate import resolve_path, _resolve_path_for_action
from tools.glob_walk import select_grep_files
from tools.grep_content import SEPARATOR, GrepLine, SourceLines, context_ranges, render
from tools.grep_ripgrep import search_ripgrep
from tools.search_scope import (
    THREADS_NOTICE, SearchScope, included_filter, keep_path, pattern_components,
    reviewed_grep_reads, search_identity,
)


def _has_ripgrep() -> bool:
    try:
        subprocess.run(["rg", "--version"], capture_output=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


_RG_AVAILABLE = _has_ripgrep()
_MAX_SEARCH_FILE_BYTES = 10 * 1024 * 1024


def _grep_scope(search_dir: Path, current_thread_id: str | None, all_threads: bool) -> SearchScope:
    """Grep's scope: the shared rules; a ``.env`` file is read only when named."""
    return SearchScope(current_thread_id, all_threads, root_name=search_dir.name)


def _add_right_stripped(
    results: list[GrepLine], safe: list[str | None], lines: list[GrepLine], faithful: list[str | None],
) -> None:
    """Add the fallback search's lines and their faithful text, right-stripped as it prints them."""
    results.extend(replace(line, text=line.text.rstrip()) for line in lines)
    safe.extend(None if text is None else text.rstrip() for text in faithful)


class GrepTool(Tool):
    """Search file contents using regex patterns."""

    OUTPUT_CHAR_CAP = GREP_OUTPUT_CHAR_CAP

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents using regex patterns. Built on ripgrep for speed.\n\n"
            "Supports full regex syntax (e.g., 'log.*Error', 'function\\s+\\w+', 'TODO|FIXME').\n"
            "Filter files with the glob parameter (e.g., '*.py', '**/*.ts').\n"
            "Skips hidden files and folders (except your own thread's .threads folder), "
            "node_modules, __pycache__ and venv unless the glob names them (for example "
            "'.github/**/*.yml'); passing one as path also searches inside it.\n"
            "Output is capped at ~20K characters. Use 'files' mode for broad searches, "
            "'content' mode with glob filters for targeted results.\n\n"
            "Output modes:\n"
            "- 'content': shows matching lines with file path and line number\n"
            "- 'files': shows only file paths that contain matches (default)\n"
            "- 'count': shows match count per file\n\n"
            "USE THIS when you need to:\n"
            "- Find where a function, class, or variable is defined or used\n"
            "- Search for specific strings or patterns across the codebase\n"
            "- Find TODO/FIXME comments, error messages, or log patterns\n"
            "- Locate imports or references to a module\n\n"
            "DO NOT use this when:\n"
            "- Finding files by name (use glob instead)\n"
            "- Reading a specific file (use read_file instead)\n"
            "- Listing directory contents (use list_dir instead)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to workspace root.",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g., '*.py', '*.{ts,tsx}')",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files", "count"],
                    "description": "Output mode: 'content' (matching lines), 'files' (file paths only, default), 'count' (match counts)",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case insensitive search. Default: false.",
                },
                "context": {
                    "type": "integer",
                    "description": "Number of context lines before and after each match. Only for 'content' mode.",
                    "minimum": 0,
                    "maximum": 10,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 50.",
                    "minimum": 1,
                    "maximum": 200,
                },
                "all_threads": {
                    "type": "boolean",
                    "description": "Also search other threads' .threads folders. Default: false — search covers shared files and your own thread's folder.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        output_mode: str = "files",
        case_insensitive: bool = False,
        context: int = 0,
        max_results: int = 50,
        all_threads: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={
                    "pattern": pattern,
                    "path": path or ".",
                    "glob": glob,
                    "output_mode": output_mode,
                    "case_insensitive": case_insensitive,
                    "context": context,
                    "max_results": max_results,
                    "all_threads": all_threads,
                },
                path=path or ".",
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=kwargs.get("_thread_id"),
                project_id=kwargs.get("_thread_project_id"),
            )
        except PermissionError as exc:
            return f"Error: {exc}"
        search_dir = authorized.resolved
        current_thread_id, all_threads = search_identity(search_dir, kwargs.get("_thread_id"), all_threads)
        if not keep_path(search_dir, current_thread_id, all_threads):
            return f"No matches for pattern '{pattern}'\n{THREADS_NOTICE}"

        if authorized.reviewed:
            try:
                # Off the event loop, exactly as ``glob`` already does. Both
                # search paths below walk the filesystem synchronously, and
                # `async def` makes the runtime assume they yield. They do not.
                return await asyncio.to_thread(
                    self._search_reviewed,
                    pattern,
                    search_dir,
                    glob,
                    output_mode,
                    case_insensitive,
                    context,
                    max_results,
                    current_thread_id,
                    all_threads,
                )
            except PermissionError as exc:
                return f"Error: {exc}"

        if not search_dir.exists():
            return f"Error: Path not found: {path or str(self._workspace)}"

        if _RG_AVAILABLE:
            return await search_ripgrep(
                pattern,
                search_dir,
                glob,
                output_mode,
                case_insensitive,
                context,
                max_results,
                _grep_scope(search_dir, current_thread_id, all_threads),
                self.OUTPUT_CHAR_CAP,
            )
        else:
            # The fallback taken when ripgrep is absent. It walks the tree in
            # pure Python, and on 2026-08-15 it froze production twice — a
            # recursive glob running ON the asyncio event loop, so the server
            # could not answer health, the UI, or any other thread until it
            # finished. py-spy caught it mid-freeze:
            #
            #     select_recursive_step (glob.py:513)
            #     _iter_python_files (tools/grep.py:535)
            #     _search_python (tools/grep.py:452)
            #     ...
            #     run (asyncio/runners.py:118)     <- the loop itself
            #
            # Installing ripgrep only makes this path rarer. One agent's search
            # must never be able to stop the platform.
            return await asyncio.to_thread(
                self._search_python,
                pattern,
                search_dir,
                glob,
                output_mode,
                case_insensitive,
                context,
                max_results,
                current_thread_id,
                all_threads,
            )

    def _search_reviewed(
        self,
        pattern: str,
        search_path: Path,
        glob_filter: str | None,
        output_mode: str,
        case_insensitive: bool,
        context: int,
        max_results: int,
        current_thread_id: str | None,
        all_threads: bool,
    ) -> str:
        """Search a reviewed host path through no-follow descriptors."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"Error: Invalid regex pattern: {exc}"

        results: list[GrepLine] = []
        safe: list[str | None] = []
        source = SourceLines()
        counts: dict[str, int] = {}
        content_result_count = 0
        kind = bound_path_kind(search_path)

        def process(relative: Path, size: int, content: str) -> None:
            nonlocal content_result_count
            if size > _MAX_SEARCH_FILE_BYTES:
                return
            rel = str(relative)
            lines = content.splitlines()
            matching_indexes = [
                index for index, line in enumerate(lines) if regex.search(line)
            ]
            if not matching_indexes:
                return
            counts[rel] = len(regex.findall(content))
            if output_mode != "content":
                return

            emitted: list[GrepLine] = []
            for start, end in context_ranges(matching_indexes, context, len(lines)):
                if content_result_count >= max_results:
                    break
                if context and (results or emitted):
                    emitted.append(SEPARATOR)
                for index in range(start, end):
                    if content_result_count >= max_results:
                        break
                    separator = ":" if regex.search(lines[index]) else "-"
                    emitted.append(GrepLine(
                        f"{'' if kind == 'file' else rel + separator}{index + 1}{separator}", lines[index], index + 1,
                    ))
                    content_result_count += 1
            results.extend(emitted)
            safe.extend(source.substitute(
                relative.suffix, content, emitted, universal=True,
            ))

        scope = _grep_scope(search_path, current_thread_id, all_threads)
        if kind == "file":
            if keep_path(search_path, current_thread_id, all_threads):
                with open_bound_text(search_path) as (handle, file_stat):
                    if file_stat.st_size <= _MAX_SEARCH_FILE_BYTES:
                        process(
                            Path(search_path.name),
                            file_stat.st_size,
                            handle.read(),
                        )
        else:
            named = pattern_components(included_filter(glob_filter))
            with walk_bound_files(
                search_path,
                skip_directory=partial(scope.prunes_reviewed_directory, components=named),
            ) as files:
                for entry in files:
                    relative = entry.relative
                    if not reviewed_grep_reads(relative, glob_filter):
                        continue
                    if entry.file_stat.st_size <= _MAX_SEARCH_FILE_BYTES:
                        process(
                            relative,
                            entry.file_stat.st_size,
                            entry.read_text(),
                        )
                    if output_mode != "content" and len(counts) >= max_results:
                        break
                    if output_mode == "content" and len(results) >= max_results:
                        break

        if output_mode == "files":
            if not counts:
                return f"No matches for pattern '{pattern}'"
            return "\n".join(sorted(counts)[:max_results])
        if output_mode == "count":
            if not counts:
                return f"No matches for pattern '{pattern}'"
            return "\n".join(
                f"{path}:{count}"
                for path, count in sorted(counts.items())[:max_results]
            )
        if not results:
            return f"No matches for pattern '{pattern}'"
        return render(results, safe, self.OUTPUT_CHAR_CAP)

    def _search_python(
        self,
        pattern: str,
        search_dir: Path,
        glob_filter: str | None,
        output_mode: str,
        case_insensitive: bool,
        context: int,
        max_results: int,
        current_thread_id: str | None,
        all_threads: bool,
    ) -> str:
        """Fallback: pure Python regex search (slower, no .gitignore respect)."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        files = list(
            self._iter_python_files(
                search_dir,
                glob_filter,
                current_thread_id,
                all_threads,
            )
        )

        results: list[GrepLine] = []
        safe: list[str | None] = []
        source = SourceLines()
        files_matched = set()

        for fpath, resolved_path in sorted(files):
            if len(results) >= max_results:
                break

            try:
                if resolved_path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                    continue
                content = resolved_path.read_text(encoding="utf-8", errors="ignore")
            except (PermissionError, OSError):
                continue

            lines = content.split("\n")
            emitted: list[GrepLine] = []
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    rel = str(fpath.relative_to(search_dir))
                    files_matched.add(rel)

                    if output_mode == "content":
                        emitted.append(GrepLine(f"{i}:" if fpath == search_dir else f"{rel}:{i}: ", line, i))
                        if len(results) + len(emitted) >= max_results:
                            break
            faithful = source.substitute(resolved_path.suffix, content, emitted)
            _add_right_stripped(results, safe, emitted, faithful)

        if output_mode == "files":
            if not files_matched:
                return f"No matches for pattern '{pattern}'"
            return "\n".join(sorted(files_matched)[:max_results])
        elif output_mode == "count":
            if not files_matched:
                return f"No matches for pattern '{pattern}'"
            counts = {}
            for fpath, resolved_path in sorted(files):
                try:
                    if resolved_path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                        continue
                    content = resolved_path.read_text(encoding="utf-8", errors="ignore")
                    c = len(regex.findall(content))
                    if c > 0:
                        rel = str(fpath.relative_to(search_dir))
                        counts[rel] = c
                except (PermissionError, OSError):
                    continue
            return "\n".join(
                f"{f}:{c}" for f, c in sorted(counts.items())[:max_results]
            )
        else:
            if not results:
                return f"No matches for pattern '{pattern}'"
            return render(results, safe, self.OUTPUT_CHAR_CAP)

    def _iter_python_files(
        self,
        search_dir: Path,
        glob_filter: str | None,
        current_thread_id: str | None,
        all_threads: bool,
    ) -> Iterator[tuple[Path, Path]]:
        """Yield display paths with authorized resolved targets for fallback reads."""
        scope = _grep_scope(search_dir, current_thread_id, all_threads)
        candidates: Iterator[Path]
        if search_dir.is_file():
            candidates = iter((search_dir,))
        else:
            candidates = (
                search_dir.joinpath(*relative)
                for relative in select_grep_files(search_dir, glob_filter, scope)
            )
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(search_dir):
                try:
                    resolved = resolve_path(
                        str(candidate), self._workspace, self._allowed_dir
                    )
                except PermissionError:
                    continue
            if resolved.is_file() and keep_path(resolved, current_thread_id, all_threads):
                yield candidate, resolved
