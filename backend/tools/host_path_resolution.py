"""Path helpers for host path policy: canonicalisation, containment, OS denials."""

from __future__ import annotations

import os
from pathlib import Path


def canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def absolute_unresolved(path: Path) -> Path:
    """Make ``path`` absolute without collapsing unresolved components."""
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def resolution_touches(root: Path, path: Path, *, loop_touches: bool = True) -> bool:
    """Return whether component-wise resolution ever enters ``root``.

    An unresolved symlink loop counts as touching unless ``loop_touches`` is false.
    """
    pending_path = absolute_unresolved(path)
    current = Path(pending_path.anchor)
    pending = list(pending_path.parts[1:])
    symlink_hops = 0

    while pending:
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue

        candidate = current / part
        if contains(root, candidate):
            return True
        try:
            target = Path(os.readlink(candidate))
        except OSError:
            current = candidate
            continue

        symlink_hops += 1
        if symlink_hops > 40:
            # An unresolved loop is not an ordinary protected path.
            return loop_touches
        if not target.is_absolute():
            target = candidate.parent / target
        pending_path = target.joinpath(*pending)
        current = Path(pending_path.anchor)
        pending = list(pending_path.parts[1:])

    return contains(root, current)


def contains(root: Path, path: Path) -> bool:
    return path == root or path.is_relative_to(root)


# macOS reports a sandbox refusal as ``<prog>: <path>: Operation not permitted``.
# Reading the path back out of the OS's own message is more reliable than
# parsing the agent's command line.
_DENIAL_SUFFIX = ": Operation not permitted"


def denied_paths_in(text: str) -> tuple[Path, ...]:
    """Return the absolute paths the OS itself named as refused in ``text``."""
    found: list[Path] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.endswith(_DENIAL_SUFFIX):
            continue
        head = line[: -len(_DENIAL_SUFFIX)]
        _, separator, candidate = head.partition(": ")
        if not separator:
            candidate = head
        candidate = candidate.strip().rstrip("/")
        if candidate.startswith("/"):
            found.append(Path(candidate))
    return tuple(dict.fromkeys(found))
