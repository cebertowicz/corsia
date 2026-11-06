"""Path helpers for host path policy: canonicalisation, containment, OS denials."""

from __future__ import annotations

import os
import re
import unicodedata
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


def containment_key(path: Path) -> str:
    """Normalize a path for the shared case- and composition-insensitive boundary."""
    return unicodedata.normalize("NFC", str(Path(path))).casefold()


def key_contains(root: str, path: str) -> bool:
    """Compare normalized keys at a path-component boundary."""
    if root == ".":
        return not path.startswith(os.sep)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def contains(root: Path, path: Path) -> bool:
    """Return whether ``path`` is ``root`` or below it, ignoring case and Unicode composition."""
    return key_contains(containment_key(root), containment_key(path))


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


# A bwrap mask refuses with ``Permission denied``, naming the path as the program was given it:
# ``cat: ../B/file: ...``, ``ls: cannot open directory '../B': ...``, ``mkdir: ... ‘../B/x’: ...``
# or ``PermissionError: [Errno 13] Permission denied: '../B/file'`` (Ubuntu 24.04, bubblewrap 0.9.0).
_LINUX_DENIAL = "Permission denied"
# A quote opens only after a non-word character, so the apostrophe in ``can't`` never opens one.
_LINUX_QUOTED = re.compile(r"(?<!\w)['‘]([^'’\r\n]+)['’]")


def linux_denied_paths_in(text: str, cwd: Path) -> tuple[Path, ...]:
    """Return the paths a Linux program named as refused in ``text``, relative ones joined to ``cwd``.

    ``cwd`` is where the command started, so a ``cd`` inside it can point a relative path elsewhere.
    """
    found: list[Path] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _LINUX_DENIAL not in line:
            continue
        candidates = _LINUX_QUOTED.findall(line)
        if not candidates and line.endswith(f": {_LINUX_DENIAL}"):
            field = line[: -len(f": {_LINUX_DENIAL}")].rpartition(": ")[2]
            candidates = [field.rpartition(" ")[2]]  # sed: can't read ../B/file
        found.extend(canonical(cwd / candidate) for candidate in candidates if candidate.strip("/"))
    return tuple(dict.fromkeys(found))
