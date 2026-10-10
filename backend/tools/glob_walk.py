"""Glob traversal that never reads folders outside the search scope.

``select_matches`` selects what ``Path.glob`` selects on the running interpreter,
but applies ``tools.search_scope`` while descending: hidden and skipped folders
are entered only when a pattern component names them, and another thread's
``.threads`` folder only with ``all_threads``. Excluded folders are never listed.
Its order is ``Path.glob``'s pre-order walk as of Python 3.11; the glob tool
sorts by modification time, so order only breaks ties.
"""

from __future__ import annotations

from collections.abc import Iterator
import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import sys

from tools.filesystem_effects import expanded_globs
from tools.search_scope import SearchScope, scoped_match

# About four times the ~77k files a pruned walk of the whole production
# workspace finds, so ordinary searches never reach it. Listing measured
# 700-830k entries/s, so a walk stopped here has used under half a second.
ENTRY_BUDGET = 300_000

# Python 3.13 changed two Path.glob rules: a trailing '**' also selects files,
# and '**' inside a longer part acts like '*' instead of being rejected.
# Mirror the running interpreter so glob answers exactly as it did.
_RECURSIVE_TAIL_SELECTS_FILES = sys.version_info >= (3, 13)
_PARTIAL_RECURSIVE_PART_IS_INVALID = sys.version_info < (3, 13)

_RECURSIVE = "**"
_MAGIC = re.compile(r"[*?[]")

Relative = tuple[str, ...]


class Traversal:
    """Work and skip accounting for one search under one scope.

    ``components`` are the pattern's parts, used to prune a full-tree walk.
    """

    def __init__(
        self,
        scope: SearchScope,
        limit: int | None = None,
        components: frozenset[str] = frozenset(),
    ) -> None:
        self.scope = scope
        self.components = components
        self.limit = ENTRY_BUDGET if limit is None else limit
        self.examined = 0
        self.stopped = False
        self.skipped = False
        self.skipped_threads = False

    def take(self) -> bool:
        """Charge one examined directory entry, refusing once the budget is spent."""
        if self.examined >= self.limit:
            self.stopped = True
            return False
        self.examined += 1
        return True

    def descends(self, parent: str, name: str) -> bool:
        """Whether ``**`` may enter a directory, remembering a refusal."""
        if self.scope.descends(parent, name):
            return True
        self.note_skipped(parent, name)
        return False

    def lists(self, parent: str, name: str) -> bool:
        """Whether a file reached by ``**`` may be returned, remembering a refusal."""
        if self.scope.lists(name):
            return True
        self.note_skipped(parent, name)
        return False

    def matches(self, parent: str, name: str, component: str) -> bool:
        """Whether a pattern component selects a name, remembering a scope refusal."""
        if self.scope.component_matches(parent, name, component):
            return True
        if fnmatch.fnmatchcase(name, component):
            self.note_skipped(parent, name)
        return False

    def prunes_directory(self, relative: Path) -> bool:
        """Whether a full-tree walk skips a directory, remembering that it did."""
        if not self.scope.prunes_directory(relative, self.components):
            return False
        parent = relative.parent.name if len(relative.parts) > 1 else self.scope.root_name
        self.note_skipped(parent, relative.name)
        return True

    def note_skipped(self, parent: str, name: str) -> None:
        """Remember that the scope excluded an entry, and whether it was a thread's."""
        if self.scope.other_thread(parent, name):
            self.skipped_threads = True
        else:
            self.skipped = True


def select_matches(root: Path, pattern: str, traversal: Traversal) -> Iterator[Relative]:
    """Yield root-relative parts of what ``root.glob(pattern)`` selects outside skipped names."""
    parts = _pattern_parts(pattern)
    if parts is None:
        # Pathlib rejects these patterns before reading the filesystem; let it
        # raise its own error for the running interpreter.
        for path in root.glob(pattern):
            yield path.relative_to(root).parts
        return
    selected = _select(str(root), (), parts, traversal)
    if parts.count(_RECURSIVE) < 2:
        yield from selected
        return
    seen: set[Relative] = set()
    for relative in selected:
        if relative not in seen:
            seen.add(relative)
            yield relative


def select_grep_files(root: Path, glob_filter: str | None, scope: SearchScope) -> Iterator[Relative]:
    """Yield, once each, the root-relative paths grep reads for its ``glob`` filter.

    The filter matches at any depth, as ripgrep's does. A positive filter selects
    what ``**/<filter>`` selects for each brace alternative, so folders and hidden
    names it names are read; a ``!`` filter selects every in-scope path it does not
    match. Like ripgrep, grep reads the whole scope, so the walk has no budget.
    """
    traversal = Traversal(scope, limit=sys.maxsize)
    if glob_filter and not glob_filter.startswith("!"):
        yield from dict.fromkeys(
            relative
            for expanded in expanded_globs(glob_filter)
            for relative in select_matches(root, f"**/{expanded}", traversal)
        )
        return
    excluded = f"**/{glob_filter[1:]}" if glob_filter else None
    for relative in select_matches(root, "**/*", traversal):
        if excluded is None or not scoped_match(relative, excluded, scope):
            yield relative


def _pattern_parts(pattern: str) -> Relative | None:
    """Split a pattern the way pathlib does, or ``None`` when pathlib rejects it."""
    if not pattern or pattern.startswith("/"):
        return None
    parts: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if part == _RECURSIVE and parts and parts[-1] == _RECURSIVE:
            continue
        if _PARTIAL_RECURSIVE_PART_IS_INVALID and part != _RECURSIVE and _RECURSIVE in part:
            raise ValueError("Invalid pattern: '**' can only be an entire path component")
        parts.append(part)
    if not parts:
        return None
    if pattern.endswith("/"):
        parts.append("")
    return tuple(parts)


def _select(
    directory: str, relative: Relative, parts: Relative, traversal: Traversal,
) -> Iterator[Relative]:
    if not parts:
        yield relative
        return
    part, rest = parts[0], parts[1:]
    if part == _RECURSIVE:
        yield from _select_recursive(directory, relative, rest, traversal)
    elif not part:
        yield relative
    elif _MAGIC.search(part):
        entries = _scan(directory, traversal)
        yield from _select_wildcard(entries, relative, part, rest, traversal)
    elif traversal.take() and traversal.matches(_parent(relative, traversal), part, part):
        # A literal part is looked up, not listed, so the filesystem decides
        # case matching exactly as it does for pathlib. Naming a part enters it.
        path = os.path.join(directory, part)
        present = os.path.isdir(path) if rest else os.path.lexists(path)
        if present:
            yield from _select(path, relative + (part,), rest, traversal)


def _select_wildcard(
    entries: list[os.DirEntry[str]],
    relative: Relative,
    part: str,
    rest: Relative,
    traversal: Traversal,
) -> Iterator[Relative]:
    parent = _parent(relative, traversal)
    for entry in entries:
        name = entry.name
        if not traversal.matches(parent, name, part):
            continue
        if rest and not _is_directory(entry, follow_symlinks=True):
            continue
        yield from _select(entry.path, relative + (name,), rest, traversal)


def _select_recursive(
    directory: str, relative: Relative, rest: Relative, traversal: Traversal,
) -> Iterator[Relative]:
    """Walk real directories pre-order, selecting ``rest`` from each."""
    stack = [(directory, relative)]
    while stack:
        current, current_relative = stack.pop()
        entries = _scan(current, traversal)
        yield from _select_below(current, current_relative, entries, rest, traversal)
        parent = _parent(current_relative, traversal)
        children = [
            (entry.path, current_relative + (entry.name,))
            for entry in entries
            if _is_directory(entry, follow_symlinks=False)
            and traversal.descends(parent, entry.name)
        ]
        stack.extend(reversed(children))


def _select_below(
    directory: str,
    relative: Relative,
    entries: list[os.DirEntry[str]],
    rest: Relative,
    traversal: Traversal,
) -> Iterator[Relative]:
    """Select ``rest`` from one directory the recursive walk already listed."""
    if not rest:
        yield relative
        if _RECURSIVE_TAIL_SELECTS_FILES:
            parent = _parent(relative, traversal)
            for entry in entries:
                if _is_directory(entry, follow_symlinks=False):
                    continue
                if traversal.lists(parent, entry.name):
                    yield relative + (entry.name,)
    elif rest[0] and _MAGIC.search(rest[0]):
        yield from _select_wildcard(entries, relative, rest[0], rest[1:], traversal)
    else:
        yield from _select(directory, relative, rest, traversal)


def _parent(relative: Relative, traversal: Traversal) -> str:
    return relative[-1] if relative else traversal.scope.root_name


def _scan(directory: str, traversal: Traversal) -> list[os.DirEntry[str]]:
    """List a directory within budget; an unreadable one selects nothing, as in pathlib."""
    entries: list[os.DirEntry[str]] = []
    if traversal.stopped:
        return entries
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if not traversal.take():
                    break
                entries.append(entry)
    except OSError:
        entries.clear()
    return entries


def _is_directory(entry: os.DirEntry[str], *, follow_symlinks: bool) -> bool:
    try:
        return entry.is_dir(follow_symlinks=follow_symlinks)
    except OSError:
        return False
