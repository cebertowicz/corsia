"""Project fence checks shared by search traversals."""

from __future__ import annotations

import os
from pathlib import Path

from tools import project_fence
from tools.host_path_resolution import contains, containment_key, key_contains
from tools.workspace_git_paths import workspace_git_history_paths

NOTICE = "Folders this agent can't read were left out."
_ESCAPE = str.maketrans({char: f"\\{char}" for char in "\\*?[]{}!"})


def _forms(path: Path) -> tuple[Path, ...]:
    return tuple(dict.fromkeys((Path(os.path.abspath(path)), path.resolve(strict=False))))


class SearchFence:
    """The fenced roots below an already-authorized search root."""

    def __init__(self, root: Path, thread_id: str | None, fence: project_fence.Fence | None = None):
        self.root = root
        self.thread_id = thread_id
        search_forms = _forms(root)
        fence = fence if fence is not None else project_fence.fence_for_thread(thread_id)
        from infra.config import WORKSPACE

        self.roots = tuple(
            fenced for fenced in fence.roots
            if not any(contains(form, search) for form in fenced.forms for search in search_forms)
        ) + tuple(project_fence.FencedRoot(path) for path in workspace_git_history_paths(WORKSPACE))
        self._root_keys = tuple(containment_key(form) for fenced in self.roots for form in fenced.forms)
        self._file_keys = tuple(containment_key(form) for fenced in self.roots for form in fenced.forms if form.is_file())
        self.has_file_exclusions = any(
            key_contains(containment_key(search), key) for search in search_forms for key in self._file_keys
        )
        self.pruned = False
        self.incomplete = False
        self.has_exclusions = any(
            contains(search, form) for fenced in self.roots
            for form in fenced.forms for search in search_forms
        )

    def excludes(self, path: Path) -> bool:
        """Refuse paths in an excluded root, in written or resolved form."""
        if not self._root_keys:
            return False
        keys = tuple(containment_key(form) for form in _forms(path.expanduser()))
        excluded = any(key_contains(root, key) for key in keys for root in self._root_keys)
        self.pruned |= excluded
        return excluded

    def excludes_directory(self, path: Path) -> bool:
        """Prune a real directory using its lexical key."""
        if not self.has_exclusions:
            return False
        key = containment_key(path)
        excluded = any(key_contains(root, key) for root in self._root_keys)
        self.pruned |= excluded
        return excluded

    def follow_symlink(self, path: Path) -> Path | None:
        """Resolve a link before traversal and activate any roots below its target."""
        resolved = path.resolve(strict=False)
        if self.excludes(path):
            return None
        key = containment_key(resolved)
        self.has_exclusions |= any(key_contains(key, root) for root in self._root_keys)
        self.has_file_exclusions |= any(key_contains(key, root) for root in self._file_keys)
        return resolved

    def excludes_file(self, path: Path) -> bool:
        """Exclude a Git marker that is itself a file."""
        if not self._file_keys:
            return False
        excluded = containment_key(path) in self._file_keys
        self.pruned |= excluded
        return excluded

    def excludes_ripgrep(self, path: Path) -> bool:
        """Filter a no-follow ripgrep record below the authorized resolved root."""
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("ripgrep path escapes its search root") from exc
        if ".." in relative.parts:
            raise ValueError("ripgrep path escapes its search root")
        key = containment_key(self.root / relative)
        excluded = any(key_contains(root, key) for root in self._root_keys)
        self.pruned |= excluded
        return excluded

    def notice(self, result: str) -> str:
        """Append the approved notice when a fenced path was excluded."""
        if not (self.has_exclusions or self.pruned):
            return result
        from host_access.credential_sanitizer import CredentialSafeText

        if isinstance(result, CredentialSafeText):
            from tools.grep_content import with_notice

            return with_notice(result, NOTICE)
        return f"{result}\n{NOTICE}"

    def ripgrep_globs(self, root: Path) -> list[str]:
        """Find physical spellings of excluded directories without entering them."""
        exclusions: set[str] = set()
        for fenced in self.roots:
            for form in fenced.forms:
                for base in _forms(root):
                    if not contains(base, form):
                        continue
                    parts = form.parts[len(base.parts):]
                    paths = [root]
                    for index, part in enumerate(parts):
                        next_paths = []
                        for parent in paths:
                            if self.excludes(parent):
                                continue
                            try:
                                with os.scandir(parent) as entries:
                                    next_paths.extend(
                                        Path(entry.path) for entry in entries
                                        if contains(Path(part), Path(entry.name))
                                        and (index == len(parts) - 1 or entry.is_dir(follow_symlinks=False))
                                    )
                            except OSError:
                                self.incomplete = True
                                relative = parent.relative_to(root).as_posix().translate(_ESCAPE)
                                exclusions.add(f"!/{relative}/" if relative != "." else "!/**")
                        paths = next_paths
                    for path in paths:
                        relative = path.relative_to(root).as_posix().translate(_ESCAPE)
                        exclusions.update((f"!/{relative}", f"!/{relative}/"))
        return sorted(exclusions)
