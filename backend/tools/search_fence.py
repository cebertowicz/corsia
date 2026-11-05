"""Project fence checks shared by search traversals."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from tools import project_fence

NOTICE = "Other projects this agent can't read were left out."
_ESCAPE = str.maketrans({char: f"\\{char}" for char in "\\*?[]{}!"})


def _key(path: Path) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", unicodedata.normalize("NFC", part).casefold()) for part in path.parts)


def _contains(parent: Path, child: Path) -> bool:
    parts = _key(parent)
    return _key(child)[:len(parts)] == parts


def _forms(path: Path) -> tuple[Path, ...]:
    return tuple(dict.fromkeys((Path(os.path.abspath(path)), path.resolve(strict=False))))


class SearchFence:
    """The fenced roots below an already-authorized search root."""

    def __init__(self, root: Path, thread_id: str | None):
        self.root = root
        self.thread_id = thread_id
        search_forms = _forms(root)
        fence = project_fence.fence_for_thread(thread_id)
        self.roots = tuple(
            fenced for fenced in fence.roots
            if not any(_contains(form, search) for form in fenced.forms for search in search_forms)
        )
        self.pruned = False
        self.has_exclusions = any(
            _contains(search, form) for fenced in self.roots
            for form in fenced.forms for search in search_forms
        )

    def excludes(self, path: Path) -> bool:
        """Refuse paths in an excluded root, in written or resolved form."""
        excluded = project_fence.Fence(self.roots, self.thread_id).matching_root(path) is not None
        self.pruned |= excluded
        return excluded

    def notice(self, result: str) -> str:
        """Append the approved notice when a fenced path was excluded."""
        if not (self.has_exclusions or self.pruned):
            return result
        from host_access.credential_sanitizer import CredentialSafeText

        text = f"{result}\n{NOTICE}"
        return CredentialSafeText(text) if isinstance(result, CredentialSafeText) else text

    def ripgrep_globs(self, root: Path) -> list[str]:
        """Find physical spellings of excluded directories without entering them."""
        exclusions: set[str] = set()
        for fenced in self.roots:
            for form in fenced.forms:
                for base in _forms(root):
                    if not _contains(base, form):
                        continue
                    parts = form.parts[len(base.parts):]
                    paths = [root]
                    for part in parts:
                        next_paths = []
                        for parent in paths:
                            if self.excludes(parent):
                                continue
                            with os.scandir(parent) as entries:
                                next_paths.extend(
                                    Path(entry.path) for entry in entries
                                    if _key(Path(entry.name)) == _key(Path(part))
                                    and entry.is_dir(follow_symlinks=False)
                                )
                        paths = next_paths
                    for path in paths:
                        relative = path.relative_to(root).as_posix().translate(_ESCAPE)
                        exclusions.add(f"!/{relative}/")
        return sorted(exclusions)
