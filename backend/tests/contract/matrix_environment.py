"""Fail-closed, inference-free subprocess environment for the contract matrix.

The supported integration runner is macOS. Seatbelt restrictions are inherited
by subprocesses, unlike a Python socket mock. Only the exact synthetic fixture
port is reachable; other loopback services are excluded too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def isolated_environment(root: Path, workspace: Path, state: Path) -> dict[str, str]:
    """Build an allowlisted environment without ambient provider/config/proxy state."""
    homes = {name: root / name for name in ("home", "codex", "claude", "cache", "tmp")}
    for path in homes.values():
        path.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/bin", "/bin")),
        "HOME": str(homes["home"]),
        "CODEX_HOME": str(homes["codex"]),
        "CLAUDE_CONFIG_DIR": str(homes["claude"]),
        "XDG_CACHE_HOME": str(homes["cache"]),
        "TMPDIR": str(homes["tmp"]),
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
        "CORSIA_E2E_MOCK": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if runner_tracking_id := os.environ.get("RUNNER_TRACKING_ID"):
        environment["RUNNER_TRACKING_ID"] = runner_tracking_id
    return environment


def guarded_command(arguments: list[str], fixture_port: int) -> list[str]:
    """Deny network access before Python/app imports and in every descendant."""
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.is_file():
        raise RuntimeError("Contract matrix requires macOS sandbox-exec; refusing unguarded inference")
    if not 1 <= fixture_port <= 65535:
        raise ValueError("Contract matrix requires an allocated fixture port")
    endpoint = f"localhost:{fixture_port}"
    profile = (
        "(version 1)(allow default)(deny network*)"
        f'(allow network-bind (local ip "{endpoint}"))'
        f'(allow network-inbound (local ip "{endpoint}"))'
        f'(allow network-outbound (remote ip "{endpoint}"))'
    )
    return [str(sandbox), "-p", profile, *arguments]
