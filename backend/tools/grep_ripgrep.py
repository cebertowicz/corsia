"""Grep through ripgrep, under the search scope of ``tools.search_scope``.

ripgrep applies ``.gitignore`` itself, and ``--glob`` overrides carry the scope's
skipped names. Thread folders need more: ``.threads`` is usually gitignored, and
only an including glob enters an ignored folder. Once any including glob is
given, ripgrep drops every file no including glob matches, and a catch-all glob
that keeps them outranks ``.gitignore`` everywhere. So the search walk never
enters ``.threads``: a listing pass first finds the allowed thread folders that
hold files, and the search names them as extra roots.

Later globs win, so the exclusions follow a filter, keeping its wildcards from
re-including skipped names, and what the filter names follows the exclusions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
import itertools
import os
from pathlib import Path
import re

from core.workspace_layout import THREADS_DIR, thread_tray_owners
from tools.filesystem_effects import expanded_globs
from tools.grep_content import SEPARATOR, GrepLine, SourceLines, read_source, render
from tools.search_scope import (
    SKIPPED_NAMES, SearchScope, included_filter, keep_path, path_components,
)

TIMEOUT_SECONDS = 30
_ERROR_CHARS = 1_000
_RIPGREP = ("rg", "--no-config", "--no-follow", "--hidden")
_SKIP_GLOBS = ("!.*", *(f"!{name}" for name in sorted(SKIPPED_NAMES)))
_GLOB_ESCAPE = str.maketrans({char: f"\\{char}" for char in "\\*?[]{}!"})
# After the NUL that ends the path: the line number, then ':' for a match or
# '-' for a context line.
_CONTENT_RE = re.compile(r"(?P<number>[0-9]+)(?P<separator>[:-])")


@dataclass(frozen=True)
class _Ripgrep:
    """One finished ripgrep run: what it printed, and what went wrong if it failed."""

    output: str
    error: str = ""


async def search_ripgrep(
    pattern: str,
    search_dir: Path,
    glob_filter: str | None,
    output_mode: str,
    case_insensitive: bool,
    context: int,
    max_results: int,
    scope: SearchScope,
    output_cap: int,
) -> str:
    """Search file contents with ripgrep and render grep's output for ``output_mode``.

    A ripgrep failure is never an empty result: with nothing found it is an error,
    and beside partial results it leads the output as ``INCOMPLETE``.
    """
    cwd = (search_dir.parent if search_dir.name == THREADS_DIR else search_dir) if scope.fence and scope.fence.has_exclusions else None
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            exclusions = await asyncio.to_thread(scope.fence.ripgrep_globs, cwd) if cwd and scope.fence else []
            roots, listing = await _search_roots(search_dir, glob_filter, scope, cwd, exclusions)
            search = await _run([
                *_RIPGREP,
                "--no-heading",
                *_mode_arguments(output_mode, case_insensitive, context),
                *_glob_arguments(_search_globs(glob_filter) + exclusions),
                "-m", str(max_results * 5),
                "-e", pattern, "--", *map(str, roots),
            ], cwd=cwd) if roots else _Ripgrep("")
    except TimeoutError:
        return f"Error: Search timed out after {TIMEOUT_SECONDS} seconds"
    except Exception as exc:
        return f"Error: {exc}"

    if search.error and not search.output.strip():
        return f"Error: ripgrep failed:\n{search.error[:_ERROR_CHARS]}"
    errors = [run.error for run in (listing, search) if run.error]
    header = (
        f"INCOMPLETE: ripgrep could not search every path "
        f"({errors[0].splitlines()[0][:_ERROR_CHARS]}); results may be partial\n\n"
    ) if errors else ""
    if output_mode == "content":
        # Reads matched files for the source path, so it runs off the event loop.
        return await asyncio.to_thread(
            _render_content, pattern, search_dir, search.output, max_results, scope,
            output_cap, header,
        )
    rendered = _render(pattern, search_dir, search.output, max_results, scope, output_cap, output_mode)
    return f"{header}{rendered}"


def _render(
    pattern: str,
    search_dir: Path,
    output: str,
    max_results: int,
    scope: SearchScope,
    output_cap: int,
    output_mode: str,
) -> str:
    """Grep's text for ripgrep's files or count output: kept lines, limited,
    root-relative and capped."""
    errors: list[str] = []
    lines = _kept_lines(output, scope, output_mode, errors)
    header = _invalid_record_header(errors)
    if not lines:
        return f"{header}No matches for pattern '{pattern}'"
    lines = lines[:max_results]
    prefix = _display(f"{search_dir}/")
    if output_mode == "count" and search_dir.is_file():
        rendered = "\n".join(line.rpartition(":")[2] for line in lines)
    else:
        rendered = "\n".join(line.replace(prefix, "") for line in lines)
    if len(rendered) > output_cap:
        rendered = (
            rendered[:output_cap]
            + f"\n\n... (output truncated at {output_cap:,} chars. Narrow your search "
            "with glob filters or use 'files' mode.)"
        )
    return f"{header}{rendered}"


async def _run(command: list[str], *, cwd: Path | None = None) -> _Ripgrep:
    """Run one ripgrep command to completion; the process never outlives the call.

    ripgrep exits 0 when it finds something, 1 when it finds nothing, and 2 when
    anything failed, possibly after printing partial results.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    output = os.fsdecode(stdout)
    if process.returncode in (0, 1):
        return _Ripgrep(output)
    error = stderr.decode("utf-8", errors="replace").strip()
    return _Ripgrep(output, error or f"ripgrep exited with status {process.returncode}")


async def _search_roots(
    search_dir: Path, glob_filter: str | None, scope: SearchScope,
    cwd: Path | None, exclusions: list[str],
) -> tuple[list[Path], _Ripgrep]:
    """``search_dir``, unless it is a ``.threads`` folder, and the allowed thread
    folders below it, with the listing run that found them."""
    roots = [] if search_dir.name == THREADS_DIR else [search_dir]
    if thread_tray_owners(search_dir) or not search_dir.is_dir():
        return roots, _Ripgrep("")
    if not scope.current_thread_id and not scope.all_threads:
        return roots, _Ripgrep("")
    listing = await _run([
        *_RIPGREP,
        "--files",
        *_glob_arguments(_thread_listing_globs(glob_filter, scope) + exclusions),
        "--", str(search_dir),
    ], cwd=cwd)
    folders = {
        folder
        for line in listing.output.splitlines()
        if (folder := _thread_folder(search_dir, Path(line))) is not None
        and keep_path(folder, scope.current_thread_id, scope.all_threads)
    }
    return [*roots, *sorted(folders)], listing


def _thread_folder(search_dir: Path, listed: Path) -> Path | None:
    """The outermost ``.threads/<id>`` folder below ``search_dir`` holding a listed file."""
    try:
        relative = listed.relative_to(search_dir).parts
    except ValueError:
        return None
    parts = (search_dir.name, *relative)
    for index in range(len(parts) - 2):
        if parts[index] == THREADS_DIR:
            return search_dir.joinpath(*relative[: index + 1])
    return None


def _mode_arguments(output_mode: str, case_insensitive: bool, context: int) -> list[str]:
    if output_mode == "files":
        arguments = ["--null", "--files-with-matches"]
    elif output_mode == "count":
        arguments = ["--null", "--count", "--with-filename"]
    else:
        arguments = ["--null", "--line-number", "--with-filename", *(["-C", str(context)] if context > 0 else [])]
    return [*arguments, *(["-i"] if case_insensitive else [])]


def _search_globs(glob_filter: str | None) -> list[str]:
    """Globs for the search walk: the filter, then the scope's skips and names."""
    included = included_filter(glob_filter)
    globs = [f"**/{included}"] if included else []
    # Thread folders are searched only as roots, so the walk never enters one.
    globs += [*_skip_globs(glob_filter), f"!{THREADS_DIR}/"]
    if glob_filter and glob_filter.startswith("!") and len(glob_filter) > 1:
        globs.append(f"!**/{glob_filter[1:]}")
    return globs


def _thread_listing_globs(glob_filter: str | None, scope: SearchScope) -> list[str]:
    """Globs listing the files in allowed thread folders under the search's own skips
    and names, so every folder holding a file the search could read is found."""
    owner = "*" if scope.all_threads else str(scope.current_thread_id).translate(_GLOB_ESCAPE)
    globs = [f"**/{THREADS_DIR}/{owner}/**", *_skip_globs(glob_filter), f"{THREADS_DIR}/"]
    if not scope.all_threads:
        globs += [f"!**/{THREADS_DIR}/*/", f"**/{THREADS_DIR}/{owner}/"]
    return globs


def _skip_globs(glob_filter: str | None) -> list[str]:
    """The scope's skipped names, then re-included where an including filter names them.

    A folder the filter names is entered wherever one is; a hidden or skipped file
    name is re-included only where the whole filter matches it.
    """
    folders: set[str] = set()
    files: set[str] = set()
    included = included_filter(glob_filter)
    for expanded in expanded_globs(included) if included else ():
        components = path_components(expanded)
        folders.update(f"{name}/" for name in components[:-1] if _is_skipped_name(name))
        if components and _is_skipped_name(components[-1]):
            files.add(f"**/{expanded}")
    return [*_SKIP_GLOBS, *sorted(folders), *sorted(files)]


def _is_skipped_name(component: str) -> bool:
    return component != "**" and (component.startswith(".") or component in SKIPPED_NAMES)


def _render_content(
    pattern: str,
    search_dir: Path,
    output: str,
    max_results: int,
    scope: SearchScope,
    output_cap: int,
    header: str,
) -> str:
    """Grep's text for ripgrep's ``--null`` content output, each line as ``read_file``
    would show it where the file has a source path."""
    errors: list[str] = []
    parsed = _first_matches(_content_lines(output, search_dir, scope, errors), max_results)
    header += _invalid_record_header(errors)
    if not parsed:
        return f"{header}No matches for pattern '{pattern}'"
    source = SourceLines()
    safe: list[str | None] = []
    for path, group in itertools.groupby(parsed, key=lambda item: item[0]):
        lines = [line for _path, line in group]
        if path is None:
            safe.extend(None for _line in lines)
        else:
            safe.extend(source.substitute(Path(path).suffix, partial(read_source, Path(path)), lines))
    displayed = [
        SEPARATOR if line is SEPARATOR else GrepLine(_display(line.prefix), _display(line.text), line.number)
        for _path, line in parsed
    ]
    return render(displayed, [None if text is None else _display(text) for text in safe], output_cap, _display(header))


def _content_lines(
    output: str, search_dir: Path, scope: SearchScope, errors: list[str],
) -> list[tuple[str | None, GrepLine]]:
    """Each content line's file and rendering, outside other threads' folders;
    empty when no match line remains.

    ``--null`` ends each path with a NUL, so a path holding ':' or '-' parses
    exactly; rendering restores grep's separators. ``--`` separates context
    groups and carries no path.
    """
    prefix = f"{search_dir}/"
    direct_file = search_dir.is_file()
    parsed: list[tuple[str | None, GrepLine]] = []
    for path, rest in _records(output, scope, "content", errors):
        if path is None:
            parsed.append((None, SEPARATOR))
            continue
        match = _CONTENT_RE.match(rest)
        if match is None:
            errors.append("invalid record")
            continue
        separator = match.group("separator")
        number = match.group("number")
        line_prefix = (
            f"{number}{separator}"
            if direct_file
            else f"{path.removeprefix(prefix)}{separator}{number}{separator}"
        )
        parsed.append((path, GrepLine(
            line_prefix, rest[match.end():], int(number),
        )))
    return parsed if any(line is not SEPARATOR for _path, line in parsed) else []


def _first_matches(
    lines: list[tuple[str | None, GrepLine]], max_results: int,
) -> list[tuple[str | None, GrepLine]]:
    """Content lines through the ``max_results``-th line, with their separators."""
    kept: list[tuple[str | None, GrepLine]] = []
    count = 0
    for item in lines:
        kept.append(item)
        if item[1] is not SEPARATOR:
            count += 1
        if count >= max_results:
            break
    return kept


def _invalid_record_header(errors: list[str]) -> str:
    return "INCOMPLETE: ripgrep returned an invalid or unfilterable record; results may be partial\n\n" if errors else ""


def _records(output: str, scope: SearchScope, mode: str, errors: list[str]):
    """Parse the complete NUL-delimited path before its record, failing closed."""
    while output:
        if mode == "content" and output.startswith("--\n"):
            output = output[3:]
            yield None, ""
            continue
        path, delimiter, output = output.partition("\0")
        if not delimiter or not path:
            errors.append("invalid or unfilterable record")
            return
        rest = ""
        if mode != "files":
            rest, newline, output = output.partition("\n")
            if not newline or (mode == "count" and not rest.isdecimal()):
                errors.append("invalid or unfilterable record")
                return
        try:
            candidate = Path(path)
            allowed = keep_path(candidate, scope.current_thread_id, scope.all_threads)
            if scope.fence and scope.fence.excludes_ripgrep(candidate):
                allowed = False
        except Exception:
            errors.append("invalid or unfilterable record")
            continue
        if allowed:
            yield path, rest


def _display(text: str) -> str:
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _kept_lines(output: str, scope: SearchScope, mode: str, errors: list[str]) -> list[str]:
    """Render only records whose complete path passed the scope checks."""
    return [_display(path + (f":{rest}" if mode == "count" else ""))
            for path, rest in _records(output, scope, mode, errors) if path is not None]


def _glob_arguments(globs: list[str]) -> list[str]:
    return [argument for glob in globs for argument in ("--glob", glob)]
