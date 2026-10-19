"""Private state-root identity and storage for CORSIA's managed oMLX."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MANAGED_DIR = "managed-omlx"
MANAGED_PORTS = range(8210, 8310)


@dataclass(frozen=True)
class ManagedOmlxPaths:
    """Filesystem and port that belong exclusively to CORSIA's managed oMLX."""

    base_path: Path
    model_dir: Path
    settings_path: Path
    port: int


def managed_omlx_paths() -> ManagedOmlxPaths:
    """Return the durable private paths and loopback port for CORSIA's oMLX."""
    from infra.config import STATE_ROOT
    from llm.providers.omlx_process import port_is_free

    base = Path(STATE_ROOT) / MANAGED_DIR
    models = base / "models"
    settings = base / "settings.json"
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
            raise RuntimeError("CORSIA couldn't reserve a private port for its local model server. Try again.")
        port = free_port
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    raw["server"] = {**server, "port": port}
    model_value = raw.get("model")
    model = model_value if isinstance(model_value, dict) else {}
    raw["model"] = {**model, "model_dir": str(models)}
    settings.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return ManagedOmlxPaths(base, models, settings, port)


def is_corsia_managed_server(server: dict, metadata: dict | None = None) -> bool:
    """Whether a row is exactly CORSIA's private managed oMLX process."""
    from infra.config import STATE_ROOT

    if (
        server.get("provider") != "omlx"
        or server.get("kind") != "managed"
        or server.get("instance_id") != "corsia-managed"
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
    base = Path(STATE_ROOT) / MANAGED_DIR
    return (
        metadata.get("base_path") == str(base)
        and metadata.get("model_dir") == str(base / "models")
    )
