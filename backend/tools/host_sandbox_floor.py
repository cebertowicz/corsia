"""Permanent shell and local-MCP boundaries shared by protected and leased profiles."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from host_access.browser_ports import sandbox_rules
from infra.api_token_file import api_token_file, api_token_folder
from tools.host_path_resolution import canonical
from tools.protected_path_rules import ancestor_pins


def stored_credential_roots(state_root: Path, home: Path) -> tuple[Path, ...]:
    """The key store, the saved browser profile and the API token folder.

    The token folder is a credential root at a fixed place under ``home``.
    """
    return (state_root / "keys", state_root / "browser-profile", canonical(api_token_folder(home)))


def credential_ancestor_pins(credential_roots: Iterable[Path], home: Path) -> tuple[Path, ...]:
    """Every folder above a credential root: a lease may write anywhere, so all stay in place.

    The token folder's ancestors are pinned both as resolved and as the CLIs
    write them, so a linked ``.corsia`` or token folder keeps its target.
    """
    roots = tuple(credential_roots)
    pins = ancestor_pins((*roots, api_token_file(home)), (Path("/"),), (Path("/"),))
    return tuple(path for path in pins if path not in roots)


def hard_floor_rules(credential_roots: Iterable[Path], home: Path, sandbox_text: Callable[[Path], str]) -> str:
    """Render the path policy's credential inventory with the managed browser boundary."""
    roots = tuple(credential_roots)
    paths = [f'(deny file-read* file-write* (subpath "{sandbox_text(path)}"))' for path in roots]
    pins = [f'(deny file-write* (literal "{sandbox_text(path)}"))' for path in credential_ancestor_pins(roots, home)]
    return "\n".join([*paths, *pins, sandbox_rules()])
