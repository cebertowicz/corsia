"""Inventory pieces of the protected shell profile that need no policy state."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from tools.host_path_resolution import canonical, contains

# Executables a protected shell may not launch directly.
DENIED_EXECUTABLES = (
    Path("/usr/bin/sudo"),
    Path("/usr/bin/su"),
    Path("/usr/local/bin/doas"),
    Path("/opt/homebrew/bin/doas"),
    Path("/sbin/shutdown"),
    Path("/sbin/reboot"),
    Path("/sbin/halt"),
    Path("/sbin/poweroff"),
    Path("/usr/bin/pmset"),
)


def ancestor_pins(
    protected: Iterable[Path], granted_roots: Iterable[Path], already_pinned: Iterable[Path],
) -> tuple[Path, ...]:
    """Directories above a protected path that a shell could rename away.

    Seatbelt checks a rename only against its source and destination, so a protected
    path is bypassed by renaming an ancestor, editing under the new name and renaming
    it back. Every ancestor inside a granted write root (the root itself included) is
    pinned, except those the profile already pins.
    """
    granted = tuple(canonical(root) for root in granted_roots)
    skip = set(already_pinned)
    return tuple(sorted({
        ancestor
        for path in protected
        for ancestor in path.parents
        if ancestor not in skip and any(contains(root, ancestor) for root in granted)
    }))
