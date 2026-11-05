"""Content search tool — search inside files using regex."""

import asyncio
import re
import subprocess
from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from infra.constants import GREP_OUTPUT_CHAR_CAP
from tools.base import Tool
from tools.filesystem_effects import bound_path_kind, open_bound_text, walk_bound_files
from tools.filesystem_gate import _resolve_path_for_action
from tools.grep_content import SEPARATOR, GrepLine, SourceLines, context_ranges, render
from tools.grep_python import iter_python_files
from tools.grep_ripgrep import search_ripgrep
from tools.grep_schema import DESCRIPTION, PARAMETERS
from tools.search_fence import SearchFence
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


def _add_right_stripped(
    results: list[GrepLine], safe: list[str | None], lines: list[GrepLine], faithful: list[str | None],
) -> None:
    """Add the fallback search's lines and their faithful text, right-stripped as it prints them."""
    results.extend(replace(line, text=line.text.rstrip()) for line in lines)
    safe.extend(None if text is None else text.rstrip() for text in faithful)


def _render_reviewed(pattern, output_mode, max_results, counts, results, safe, output_cap):
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
    return render(results, safe, output_cap)


def _python_counts(files, search_dir, regex, max_results, fence):
    counts = {}
    for fpath, resolved_path in sorted(files):
        try:
            resolved_path = resolved_path.resolve(strict=False)
            if fence and fence.excludes(resolved_path):
                continue
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
        return DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(PARAMETERS)

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
            authorized = await self._authorize(
                pattern, path, glob, output_mode, case_insensitive, context,
                max_results, all_threads, kwargs,
            )
        except PermissionError as exc:
            return f"Error: {exc}"
        search_dir, fence, current_thread_id, all_threads = await self._search_setup(authorized, kwargs, all_threads)
        if not keep_path(search_dir, current_thread_id, all_threads):
            return fence.notice(f"No matches for pattern '{pattern}'\n{THREADS_NOTICE}")

        if authorized.reviewed:
            try:
                # Off the event loop, exactly as ``glob`` already does. Both
                # search paths below walk the filesystem synchronously, and
                # `async def` makes the runtime assume they yield. They do not.
                result = await asyncio.to_thread(
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
                    fence,
                )
                return fence.notice(result)
            except PermissionError as exc:
                return f"Error: {exc}"

        if not search_dir.exists():
            return f"Error: Path not found: {path or str(self._workspace)}"

        if _RG_AVAILABLE:
            result = await search_ripgrep(
                pattern,
                search_dir,
                glob,
                output_mode,
                case_insensitive,
                context,
                max_results,
                SearchScope(current_thread_id, all_threads, root_name=search_dir.name, fence=fence),
                self.OUTPUT_CHAR_CAP,
            )
            return fence.notice(result)
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
        result = await asyncio.to_thread(
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
            fence,
            authorized.fence,
        )
        return fence.notice(result)

    @staticmethod
    async def _search_setup(authorized, kwargs, all_threads):
        search_dir = authorized.resolved
        fence = await asyncio.to_thread(SearchFence, search_dir, kwargs.get("_thread_id"), authorized.fence)
        current_thread_id, all_threads = search_identity(search_dir, kwargs.get("_thread_id"), all_threads)
        return search_dir, fence, current_thread_id, all_threads

    async def _authorize(self, pattern, path, glob, output_mode, case_insensitive, context, max_results, all_threads, kwargs):
        return await _resolve_path_for_action(
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
            fence=kwargs.get("_host_access_fence"),
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
        fence: SearchFence | None = None,
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

        scope = SearchScope(current_thread_id, all_threads, root_name=search_path.name, fence=fence)
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

        return _render_reviewed(pattern, output_mode, max_results, counts, results, safe, self.OUTPUT_CHAR_CAP)

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
        fence: SearchFence | None = None,
        project_fence=None,
    ) -> str:
        """Fallback: pure Python regex search (slower, no .gitignore respect)."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        files = list(
            iter_python_files(
                search_dir,
                glob_filter,
                current_thread_id,
                all_threads,
                fence,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                project_fence=project_fence,
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
                resolved_path = resolved_path.resolve(strict=False)
                if fence and fence.excludes(resolved_path):
                    continue
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
            return _python_counts(files, search_dir, regex, max_results, fence)
        else:
            if not results:
                return f"No matches for pattern '{pattern}'"
            return render(results, safe, self.OUTPUT_CHAR_CAP)
