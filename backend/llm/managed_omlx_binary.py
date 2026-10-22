"""Canonical identity, executable and private paths for CORSIA's managed oMLX."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from infra.platform_paths import app_bundle_root

OMLX_FORMULA = "jundot/omlx/omlx"
MANAGED_DIR = "managed-omlx"
MANAGED_INSTANCE_ID = "corsia-managed"
MANAGED_PORTS = range(8210, 8310)


def is_corsia_managed_server(server: dict) -> bool:
    """Whether this is CORSIA's private managed oMLX server, not a person's server."""
    return (
        server.get("provider") == "omlx"
        and server.get("kind") == "managed"
        and server.get("source") == "corsia"
        and server.get("instance_id") == MANAGED_INSTANCE_ID
    )


def managed_omlx_executable(path: str | Path | None = None) -> str | None:
    """Return the bundled executable by absolute path, or None when it is unavailable."""
    bundle = app_bundle_root(path)
    if bundle is None:
        return None
    executable = bundle / "Contents" / "Resources" / "bin" / "omlx"
    return str(executable) if executable.is_file() else None


def managed_omlx_install_error(path: str | Path | None = None) -> str | None:
    """Describe a damaged packaged install without turning a source read into an exception."""
    bundle = app_bundle_root(path)
    if bundle is None:
        return None
    executable = bundle / "Contents" / "Resources" / "bin" / "omlx"
    if executable.is_file():
        return None
    from llm.local_setup_results import setup_fix

    return setup_fix("damaged_bundle")


def is_managed_omlx_executable(path: str | Path) -> bool:
    """Whether a path resolves to the executable shipped in this app bundle."""
    try:
        executable = managed_omlx_executable()
        return executable is not None and Path(path).resolve() == Path(executable).resolve()
    except (RuntimeError, OSError):
        return False


def command_output(command: list[str]) -> str:
    """Run a bounded binary-ownership inspection."""
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


@dataclass(frozen=True)
class ManagedOmlxPaths:
    """Filesystem locations belonging exclusively to the managed server."""
    base_path: Path
    model_dir: Path
    settings_path: Path


class ManagedPortUnavailable(RuntimeError):
    """No private port can currently be bound."""


def managed_omlx_paths() -> ManagedOmlxPaths:
    """Look up private paths without creating directories or probing the host."""
    from infra.config import STATE_ROOT

    base = Path(STATE_ROOT) / MANAGED_DIR
    return ManagedOmlxPaths(base, base / "models", base / "settings.json")


def reserve_managed_omlx_port() -> tuple[ManagedOmlxPaths, int]:
    """Prepare private directories and record a port for the managed launch."""
    from llm.providers.omlx_process import port_is_free

    paths = managed_omlx_paths()
    base, models, settings = paths.base_path, paths.model_dir, paths.settings_path
    try:
        raw = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    server_value = raw.get("server")
    server = server_value if isinstance(server_value, dict) else {}
    configured = server.get("port")
    if isinstance(configured, int) and configured in MANAGED_PORTS:
        from llm.providers.omlx_process import PORT_OWNER_UNKNOWN, port_owner_pid
        from llm.providers.omlx_supervisor import is_corsia_process

        owner = port_owner_pid("127.0.0.1", configured)
        if owner is None or (owner is not PORT_OWNER_UNKNOWN and is_corsia_process(owner)):
            port = configured
        else:
            configured = None
    if not isinstance(configured, int) or configured not in MANAGED_PORTS:
        free_port = next((candidate for candidate in MANAGED_PORTS if port_is_free(candidate)), None)
        if free_port is None:
            raise ManagedPortUnavailable("CORSIA couldn't reserve a private port for its local model server. Try again.")
        port = free_port
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    raw["server"] = {**server, "port": port}
    model_value = raw.get("model")
    model = model_value if isinstance(model_value, dict) else {}
    raw["model"] = {**model, "model_dir": str(models)}
    settings.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return paths, port
