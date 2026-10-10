"""What the agent file-search tools, glob and grep, search.

Default scope is shared workspace and project files plus the current thread's own
``.threads/<id>`` folder. Another thread's ``.threads/<id>`` folder belongs to that
thread: ``all_threads=True`` opts in, including when a search starts inside one.

Hidden files and folders, ``node_modules``, ``__pycache__`` and ``venv`` are not
searched unless the search names them, the way a shell glob treats dotfiles: a
pattern component that starts with ``.`` may match a hidden name, and a literal
component may match any skipped name. ``**`` and ordinary wildcards match neither,
except that ``**`` enters ``.threads`` folders to reach the current thread's own
folder. Glob also lists ``.env`` files; grep reads one only when its filter names
it. Grep through ripgrep also honors ``.gitignore`` outside thread folders.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from core.workspace_layout import THREADS_DIR, thread_tray_owners
from tools.filesystem_effects import expanded_globs, match_relative_glob

SKIPPED_NAMES = frozenset({"node_modules", "__pycache__", "venv"})
# A reviewed host grep searches the hidden folders its reviewed path holds, as it
# always has; it still skips these unless its filter names them.
_REVIEWED_GREP_SKIPPED = frozenset(
    {".git", "node_modules", "__pycache__", "venv", ".venv", ".tox", ".worktrees"}
)
THREADS_NOTICE = (
    "Not searched: other threads' .threads folders. Pass all_threads=true to "
    "include them."
)
_WILDCARD = frozenset("*?[{")


def keep_path(path: Path, current_thread_id: str | None, all_threads: bool) -> bool:
    """Whether a search may return ``path``: it lies in no other thread's folder."""
    return all_threads or all(
        owner == current_thread_id for owner in thread_tray_owners(path)
    )


def _skipped(name: str) -> bool:
    return name.startswith(".") or name in SKIPPED_NAMES


def _is_wildcard(component: str) -> bool:
    return any(char in _WILDCARD for char in component)


@dataclass(frozen=True)
class SearchScope:
    """The folders one glob or grep may enter and the names it may match.

    ``root_name`` is the searched directory's own name, so a search started on a
    ``.threads`` folder still sees its children as thread folders.
    """

    current_thread_id: str | None
    all_threads: bool
    root_name: str = ""
    lists_env: bool = False

    def other_thread(self, parent: str, name: str) -> bool:
        """Whether ``name`` is another thread's folder inside a ``.threads`` folder."""
        return (
            parent == THREADS_DIR
            and not self.all_threads
            and name != self.current_thread_id
        )

    def descends(self, parent: str, name: str) -> bool:
        """Whether ``**`` or an unnamed walk may enter directory ``name``."""
        if self.other_thread(parent, name):
            return False
        return name == THREADS_DIR or not _skipped(name)

    def lists(self, name: str) -> bool:
        """Whether a file reached by ``**`` or an unnamed walk may be returned."""
        return (name == ".env" and self.lists_env) or not _skipped(name)

    def component_matches(self, parent: str, name: str, component: str) -> bool:
        """Whether one explicit pattern component selects ``name`` under ``parent``."""
        if self.other_thread(parent, name) or not fnmatch.fnmatchcase(name, component):
            return False
        if not _is_wildcard(component):
            return True
        if name.startswith("."):
            return component.startswith(".") or (name == ".env" and self.lists_env)
        return name not in SKIPPED_NAMES

    def prunes_reviewed_directory(self, relative: Path, components: frozenset[str]) -> bool:
        """Whether a reviewed host grep skips a directory: another thread's, or an
        unnamed cache, environment or worktree folder."""
        parent = relative.parent.name if len(relative.parts) > 1 else self.root_name
        name = relative.name
        if self.other_thread(parent, name):
            return True
        return name in _REVIEWED_GREP_SKIPPED and not self._named(parent, name, components)

    def prunes_directory(self, relative: Path, components: frozenset[str]) -> bool:
        """Whether a full-tree walk may skip a directory no pattern component reaches.

        ``components`` are the pattern's parts; the exact positional check is
        ``scoped_match``, applied to every file the walk yields.
        """
        parent = relative.parent.name if len(relative.parts) > 1 else self.root_name
        name = relative.name
        return not self.descends(parent, name) and not self._named(parent, name, components)

    def _named(self, parent: str, name: str, components: frozenset[str]) -> bool:
        return any(
            component != "**" and self.component_matches(parent, name, component)
            for component in components
        )


def path_components(pattern: str) -> tuple[str, ...]:
    """Split one brace-free pattern as pathlib does: no empty or ``.`` components."""
    return tuple(
        part for part in pattern.replace("\\", "/").split("/") if part not in ("", ".")
    )


def included_filter(glob_filter: str | None) -> str:
    """A grep ``glob`` filter when it includes files; ``""`` for none or a ``!`` filter."""
    return "" if not glob_filter or glob_filter.startswith("!") else glob_filter


def reviewed_grep_reads(relative: Path, glob_filter: str | None) -> bool:
    """Whether a reviewed host grep reads a file its walk reached.

    That walk enters hidden folders, as it always has, but reads a hidden file only
    where an including filter names it. A ``!`` filter reads every other file.
    """
    hidden = relative.name.startswith(".")
    included = included_filter(glob_filter)
    if not included:
        excluded = (glob_filter or "")[1:]
        return not hidden and not (excluded and match_relative_glob(relative, f"**/{excluded}"))
    return any(
        match_relative_glob(relative, f"**/{expanded}")
        and (not hidden or any(name.startswith(".") for name in path_components(expanded)[-1:]))
        for expanded in expanded_globs(included)
    )


def pattern_components(pattern: str) -> frozenset[str]:
    """Every path component of a pattern, across its brace alternatives."""
    return frozenset(
        component
        for expanded in expanded_globs(pattern)
        for component in path_components(expanded)
    )


def scoped_match(parts: tuple[str, ...], pattern: str, scope: SearchScope) -> bool:
    """Match root-relative ``parts`` against ``pattern`` under the scope's naming rules."""
    return any(
        _scoped_match(parts, path_components(expanded), scope)
        for expanded in expanded_globs(pattern)
    )


def _closed(positions: set[int], components: tuple[str, ...]) -> set[int]:
    """Add the positions a ``**`` reaches by matching no path component."""
    closed = set(positions)
    pending = list(positions)
    while pending:
        position = pending.pop()
        if position < len(components) and components[position] == "**":
            if position + 1 not in closed:
                closed.add(position + 1)
                pending.append(position + 1)
    return closed


def _scoped_match(
    parts: tuple[str, ...], components: tuple[str, ...], scope: SearchScope,
) -> bool:
    positions = _closed({0}, components)
    for index, name in enumerate(parts):
        parent = parts[index - 1] if index else scope.root_name
        last = index == len(parts) - 1
        advanced: set[int] = set()
        for position in positions:
            if position == len(components):
                continue
            component = components[position]
            if component == "**":
                if scope.lists(name) if last else scope.descends(parent, name):
                    advanced.add(position)
            elif scope.component_matches(parent, name, component):
                advanced.add(position + 1)
        positions = _closed(advanced, components)
        if not positions:
            return False
    return len(components) in positions
