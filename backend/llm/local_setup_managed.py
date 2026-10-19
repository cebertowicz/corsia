"""Private state-root identity and storage for CORSIA's managed oMLX."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

MANAGED_DIR = "managed-omlx"
MANAGED_INSTANCE_ID = "corsia-managed"
MANAGED_PORTS = range(8210, 8310)


@dataclass(frozen=True)
class ManagedOmlxPaths:
    """Filesystem locations that belong exclusively to CORSIA's managed oMLX."""

    base_path: Path
    model_dir: Path
    settings_path: Path


class ManagedPortUnavailable(RuntimeError):
    """No port in CORSIA's private range can be bound for its oMLX right now."""


def managed_omlx_paths() -> ManagedOmlxPaths:
    """Return the durable private paths for CORSIA's oMLX.

    A pure lookup: it creates, binds and writes nothing, so any reader (startup
    included) may call it. ``reserve_managed_omlx_port`` prepares a launch.
    """
    from infra.config import STATE_ROOT

    base = Path(STATE_ROOT) / MANAGED_DIR
    return ManagedOmlxPaths(base, base / "models", base / "settings.json")


def reserve_managed_omlx_port() -> tuple[ManagedOmlxPaths, int]:
    """Create the private directories and keep a loopback port for a launch.

    Only starting the managed server calls this. A port kept in its settings
    stays; otherwise the first bindable port in ``MANAGED_PORTS`` is recorded.
    Raises ``ManagedPortUnavailable`` when none can be bound.
    """
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
        port = configured
    else:
        free_port = next((candidate for candidate in MANAGED_PORTS if port_is_free(candidate)), None)
        if free_port is None:
            raise ManagedPortUnavailable(
                "CORSIA couldn't reserve a private port for its local model server. Try again.")
        port = free_port
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    raw["server"] = {**server, "port": port}
    model_value = raw.get("model")
    model = model_value if isinstance(model_value, dict) else {}
    raw["model"] = {**model, "model_dir": str(models)}
    settings.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return paths, port


def is_loopback_host(host: object) -> bool:
    """Whether ``host`` names this Mac only: ``localhost`` or a loopback address."""
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def is_retired_local_omlx(server: dict, hosts: list[str]) -> bool:
    """Whether an oMLX row names a process on this Mac that CORSIA does not own.

    CORSIA routes only its private managed oMLX on this Mac. An
    oMLX the person added on another machine (any non-loopback host) and an
    explicit External provider stay theirs. ``hosts`` are the row's known
    endpoint hosts; a row with none is judged by its ``host:port`` instance id.
    """
    if server.get("provider") != "omlx" or server.get("kind") == "external":
        return False
    # Setup's own row is rewritten in place whenever setup registers it.
    if server.get("instance_id") == MANAGED_INSTANCE_ID:
        return False
    if not hosts:
        instance = str(server.get("instance_id") or "")
        hosts = [instance.rpartition(":")[0]] if ":" in instance else []
    return bool(hosts) and all(is_loopback_host(host) for host in hosts)


def is_corsia_managed_server(server: dict, metadata: dict | None = None) -> bool:
    """Whether a row is exactly CORSIA's private managed oMLX process."""
    if (
        server.get("provider") != "omlx"
        or server.get("kind") != "managed"
        or server.get("instance_id") != MANAGED_INSTANCE_ID
    ):
        return False
    if metadata is None:
        raw = server.get("metadata_json")
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            return False
    if not isinstance(metadata, dict) or metadata.get("corsia_managed") is not True:
        return False
    paths = managed_omlx_paths()
    return (
        metadata.get("base_path") == str(paths.base_path)
        and metadata.get("model_dir") == str(paths.model_dir)
    )
