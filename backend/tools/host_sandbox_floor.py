"""Permanent shell and local-MCP boundaries shared by protected and leased profiles."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from host_access.browser_ports import sandbox_rules


def hard_floor_rules(credential_roots: Iterable[Path], sandbox_text: Callable[[Path], str]) -> str:
    """Render the path policy's credential inventory with the managed browser boundary."""
    paths = [f'(deny file-read* file-write* (subpath "{sandbox_text(path)}"))' for path in credential_roots]
    return "\n".join([*paths, sandbox_rules()])
