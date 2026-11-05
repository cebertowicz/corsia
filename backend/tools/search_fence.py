"""Project fence checks shared by search traversals."""

from __future__ import annotations

import os
from pathlib import Path

from tools import project_fence
from tools.host_path_resolution import contains, containment_key, key_contains

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
        self.roots = tuple(
            fenced for fenced in fence.roots
            if not any(contains(form, search) for form in fenced.forms for search in search_forms)
        )
        self._root_keys = tuple(containment_key(form) for fenced in self.roots for form in fenced.forms)
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

    def excludes_ripgrep(self, path: Path) -> bool:
        """Filter a no-follow ripgrep record below the authorized resolved root."""
        try:
            relative = path.relative_to(self.root)
            if ".." in relative.parts:
                raise ValueError("ripgrep path escapes its search root")
        except ValueError as exc:
            raise ValueError("ripgrep path escapes its search root") from exc
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
                    for part in parts:
                        next_paths = []
                        for parent in paths:
                            if self.excludes(parent):
                                continue
                            try:
                                with os.scandir(parent) as entries:
                                    next_paths.extend(
                                        Path(entry.path) for entry in entries
                                        if contains(Path(part), Path(entry.name))
                                        and entry.is_dir(follow_symlinks=False)
                                    )
                            except OSError:
                                self.incomplete = True
                                relative = parent.relative_to(root).as_posix().translate(_ESCAPE)
                                exclusions.add(f"!/{relative}/" if relative != "." else "!/**")
                        paths = next_paths
                    for path in paths:
                        relative = path.relative_to(root).as_posix().translate(_ESCAPE)
                        exclusions.add(f"!/{relative}/")
        return sorted(exclusions)
