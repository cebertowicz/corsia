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


#: Files v0.4's "Use mine" and "manage my oMLX" choices left in the state root.
_LEGACY_ADOPTION_RECORDS = ("own-omlx-management.json", "own-omlx-use-mine")


def is_retired_local_omlx(server: dict) -> bool:
    """Whether an oMLX row is one an older setup registered on its own.

    v0.4 setup registered its loopback oMLX as a manual ``This Mac`` row
    (origin/dev local_setup_runtime.register_loopback_server): CORSIA's old
    service on its port behind the install marker, or the person's oMLX that
    "Use mine" or "manage my oMLX" adopted, behind that choice's record. Those
    rows retire. A server the person added by hand under another name or port,
    one on another machine, and an External provider stay theirs.
    """
    if (
        server.get("provider") != "omlx"
        or server.get("kind") == "external"
        or server.get("source") != "manual"
    ):
        return False
    from infra.config import STATE_ROOT
    from llm.local_setup_ownership import marker_path
    from llm.local_setup_runtime import LOCAL_SERVER_NAME
    from llm.local_setup_upgrade import legacy_omlx_port

    instance_id = str(server.get("instance_id") or "")
    if server.get("instance_name") != LOCAL_SERVER_NAME or not instance_id.startswith("127.0.0.1:"):
        return False
    if marker_path().exists() and instance_id == f"127.0.0.1:{legacy_omlx_port()}":
        return True
    return any((Path(STATE_ROOT) / name).exists() for name in _LEGACY_ADOPTION_RECORDS)


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
