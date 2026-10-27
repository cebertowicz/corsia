"""Private configuration for the backend's native browser clients."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


def prepare_config(socket_dir: Path) -> Path:
    """Override user/project discovery with a backend-owned empty configuration."""
    path = socket_dir / "config.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    return path


def client_environment(source: Mapping[str, str], socket_dir: Path, stream_port: int | None) -> dict[str, str]:
    """Do not inherit restore, auto-save, provider or plugin settings into the daemon."""
    env = {key: value for key, value in source.items() if not key.startswith("AGENT_BROWSER_")}
    env["AGENT_BROWSER_SOCKET_DIR"] = str(socket_dir)
    # The daemon must finish ordinary actions before the shortest client probe (10s).
    env["AGENT_BROWSER_DEFAULT_TIMEOUT"] = "8000"
    if stream_port is not None:
        env["AGENT_BROWSER_STREAM_PORT"] = str(stream_port)
    return env
