"""Permanent shell and local-MCP boundaries shared by protected and leased profiles."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from host_access.browser_ports import sandbox_rules
from tools.protected_path_rules import ancestor_pins


def credential_ancestor_pins(credential_roots: Iterable[Path]) -> tuple[Path, ...]:
    """Every folder above a credential root: a lease may write anywhere, so all stay in place."""
    return ancestor_pins(credential_roots, (Path("/"),), (Path("/"),))


def hard_floor_rules(credential_roots: Iterable[Path], sandbox_text: Callable[[Path], str]) -> str:
    """Render the path policy's credential inventory with the managed browser boundary."""
    roots = tuple(credential_roots)
    paths = [f'(deny file-read* file-write* (subpath "{sandbox_text(path)}"))' for path in roots]
    pins = [f'(deny file-write* (literal "{sandbox_text(path)}"))' for path in credential_ancestor_pins(roots)]
    return "\n".join([*paths, *pins, sandbox_rules()])
