"""The default port is defined once per runtime; every runtime's copy agrees."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from infra.default_port import DEFAULT_BACKEND_PORT

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend" / "scripts"))

import corsia_api  # noqa: E402


def _shell_default(relative: str, pattern: str) -> int:
    match = re.search(pattern, (_REPO / relative).read_text(), re.MULTILINE)
    assert match, f"{relative} does not define the default port"
    return int(match.group(1))


def test_standalone_cli_default_matches_backend() -> None:
    assert corsia_api.DEFAULT_PORT == DEFAULT_BACKEND_PORT
    assert corsia_api.DEFAULT_BASE == f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}"


def test_shell_entry_point_default_matches_backend() -> None:
    assert _shell_default("scripts/corsia", r"^DEFAULT_BACKEND_PORT=(\d+)$") == DEFAULT_BACKEND_PORT


def test_installer_default_matches_backend() -> None:
    assert (
        _shell_default("scripts/install.sh", r'^BACKEND_PORT="\$\{BACKEND_PORT:-(\d+)\}"$')
        == DEFAULT_BACKEND_PORT
    )


def test_harbor_reserved_ports_name_the_backend_default() -> None:
    sys.path.insert(0, str(_REPO))
    from benchmarks.harbor.disposable import RESERVED_PORTS

    assert DEFAULT_BACKEND_PORT in RESERVED_PORTS


def test_default_is_not_a_well_known_local_tool_port() -> None:
    assert DEFAULT_BACKEND_PORT not in {3000, 3001, 5173, 8000, 8080}
