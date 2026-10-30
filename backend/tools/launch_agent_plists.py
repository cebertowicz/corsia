"""CORSIA launch-agent plists: one pattern for the Python gate and the Seatbelt rule."""

from __future__ import annotations

from pathlib import Path
import re

_STEM = "corsia"
_PLIST_RE = re.compile(rf"{_STEM}.*\.plist$", re.IGNORECASE)
# Seatbelt regexes have no case-insensitive flag, so spell out each letter's cases.
_SEATBELT_STEM = "".join(f"[{c.upper()}{c.lower()}]" for c in _STEM)


def is_corsia_plist(name: str) -> bool:
    """Whether a LaunchAgents file name belongs to CORSIA."""
    return _PLIST_RE.search(name) is not None


def corsia_plist_deny_rule(launch_agents: str) -> str:
    """Render the write deny for every CORSIA plist directly in ``launch_agents``."""
    return f'(deny file-write* (regex #"^{launch_agents}/[^/]*{_SEATBELT_STEM}[^/]*\\.plist$"))'
