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
from pathlib import Path

from core.workspace_layout import THREADS_DIR, thread_tray_owners
from tools.filesystem_effects import expanded_globs
from tools.search_scope import (
    SKIPPED_NAMES, SearchScope, included_filter, keep_path, path_components,
)

TIMEOUT_SECONDS = 30
_ERROR_CHARS = 1_000
_RIPGREP = ("rg", "--no-config", "--no-follow", "--hidden")
_SKIP_GLOBS = ("!.*", *(f"!{name}" for name in sorted(SKIPPED_NAMES)))
_GLOB_ESCAPE = str.maketrans({char: f"\\{char}" for char in "\\*?[]{}!"})


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
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            roots, listing = await _search_roots(search_dir, glob_filter, scope)
            search = await _run([
                *_RIPGREP,
                "--no-heading",
                *_mode_arguments(output_mode, case_insensitive, context),
                *_glob_arguments(_search_globs(glob_filter)),
                "-m", str(max_results * 5),
                "-e", pattern, "--", *map(str, roots),
            ]) if roots else _Ripgrep("")
    except TimeoutError:
        return f"Error: Search timed out after {TIMEOUT_SECONDS} seconds"
    except Exception as exc:
        return f"Error: {exc}"

    if search.error and not search.output.strip():
        return f"Error: ripgrep failed:\n{search.error[:_ERROR_CHARS]}"
    rendered = _render(pattern, search_dir, search.output, output_mode, max_results, scope, output_cap)
    errors = [run.error for run in (listing, search) if run.error]
    if not errors:
        return rendered
    first = errors[0].splitlines()[0][:_ERROR_CHARS]
    return (
        f"INCOMPLETE: ripgrep could not search every path ({first}); "
        f"results may be partial\n\n{rendered}"
    )


def _render(
    pattern: str,
    search_dir: Path,
    output: str,
    output_mode: str,
    max_results: int,
    scope: SearchScope,
    output_cap: int,
) -> str:
    """Grep's text for ripgrep output: kept lines, limited, root-relative and capped."""
    lines = _kept_lines(output.strip(), scope)
    if not lines:
        return f"No matches for pattern '{pattern}'"
    if output_mode in ("files", "count"):
        lines = lines[:max_results]
    else:
        lines = _first_matches(lines, max_results)
    prefix = f"{search_dir}/"
    rendered = "\n".join(line.replace(prefix, "") for line in lines)
    if len(rendered) > output_cap:
        rendered = (
            rendered[:output_cap]
            + f"\n\n... (output truncated at {output_cap:,} chars. Narrow your search "
            "with glob filters or use 'files' mode.)"
        )
    return rendered


async def _run(command: list[str]) -> _Ripgrep:
    """Run one ripgrep command to completion; the process never outlives the call.

    ripgrep exits 0 when it finds something, 1 when it finds nothing, and 2 when
    anything failed, possibly after printing partial results.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    output = stdout.decode("utf-8", errors="replace")
    if process.returncode in (0, 1):
        return _Ripgrep(output)
    error = stderr.decode("utf-8", errors="replace").strip()
    return _Ripgrep(output, error or f"ripgrep exited with status {process.returncode}")


async def _search_roots(
    search_dir: Path, glob_filter: str | None, scope: SearchScope,
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
        *_glob_arguments(_thread_listing_globs(glob_filter, scope)),
        "--", str(search_dir),
    ])
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
        arguments = ["--files-with-matches"]
    elif output_mode == "count":
        arguments = ["--count"]
    else:
        arguments = ["--line-number", *(["-C", str(context)] if context > 0 else [])]
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


def _first_matches(lines: list[str], max_results: int) -> list[str]:
    """Content lines through the ``max_results``-th match, with their separators."""
    kept: list[str] = []
    count = 0
    for line in lines:
        kept.append(line)
        if not line.startswith("--"):
            count += 1
        if count >= max_results:
            break
    return kept


def _kept_lines(output: str, scope: SearchScope) -> list[str]:
    """Output lines outside other threads' folders; empty when no match line remains.

    The globs and roots already exclude those folders; this keeps out a result that
    reached one anyway. The path before the first ':' carries the thread segments,
    and ``--`` context separators carry no path.
    """
    lines = [
        line
        for line in output.split("\n")
        if line
        and (
            line.startswith("--")
            or keep_path(Path(line.split(":", 1)[0]), scope.current_thread_id, scope.all_threads)
        )
    ]
    return lines if any(line != "--" for line in lines) else []


def _glob_arguments(globs: list[str]) -> list[str]:
    return [argument for glob in globs for argument in ("--glob", glob)]
