"""Resolve the configuration of a supervisor-owned oMLX launch."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from llm.providers.omlx_endpoints import _resolve_endpoint as _resolve_endpoint_via_freshness

log = logging.getLogger("llm.providers.omlx.supervisor")


def resolve_settings(cfg: dict) -> dict:
    """Merge row metadata and stored credentials, with explicit configuration winning."""
    sid = cfg.get("server_id")
    base: dict = {}
    if sid:
        try:
            from infra.key_store_servers import resolve_server
            from llm.network_discovery import get_server_metadata
            base = get_server_metadata(sid) or {}
            if stored_key := resolve_server(sid):
                base["api_key"] = stored_key
        except Exception as e:
            log.warning("server metadata lookup failed for %s: %s", sid, e)
            base = {}
    for key in ("api_key", "model_dir"):
        if cfg.get(key) is not None:
            base[key] = cfg[key]
    return base


def _resolve_stale_endpoint(server_id: str, host: str | None, port: int | str | None) -> tuple[str | None, int | None]:
    try:
        from llm.network_discovery import get_server
        from llm.network_endpoints import get_endpoints
        server = get_server(server_id)
        if not server:
            return None, None
        port = port or (int(server["port"]) if server.get("port") else None)
        if not host:
            host = next((ep["host"] for ep in get_endpoints(server_id) or []
                         if ep.get("host") in ("127.0.0.1", "localhost")), "127.0.0.1")
    except Exception as e:
        log.warning("endpoint resolution failed for %s: %s", server_id, e)
    return host, int(port) if port else None


def resolve_endpoint(cfg: dict) -> tuple[str | None, int | None]:
    """Resolve host and port, including offline rows with stale endpoints."""
    host, port = cfg.get("host"), cfg.get("port")
    if host and port:
        return host, int(port)
    fresh_host, fresh_port = _resolve_endpoint_via_freshness(cfg)
    if fresh_host and fresh_port:
        return fresh_host, int(fresh_port)
    server_id = cfg.get("server_id")
    return _resolve_stale_endpoint(server_id, host, port) if server_id else (None, None)


def listen_host(resolved: str) -> str:
    """Keep the oMLX listener within the install's chosen network exposure."""
    from infra.bind_host import LOCAL_BIND, BindHostError, resolve_bind_host
    from infra.config import BOOTSTRAP_CONFIG_PATH

    try:
        bind = resolve_bind_host(BOOTSTRAP_CONFIG_PATH)
    except (BindHostError, OSError) as exc:
        log.warning("oMLX bind host unreadable (%s); using %s", exc, LOCAL_BIND)
        bind = LOCAL_BIND
    return LOCAL_BIND if bind == LOCAL_BIND else resolved


def _ssd_cache_folder(settings_path: Path, default: Path) -> str | None:
    """The SSD cache folder oMLX's own settings name, ``default`` when they name none, None when off.

    A folder the person chose is kept. Only the default one oMLX saved for an earlier
    base folder (``<base_path>/cache``) follows the current base, so a moved state
    root never leaves the server writing into the old one.
    """
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    cache = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
    if cache.get("enabled") is False:
        return None
    chosen, saved_base = cache.get("ssd_cache_dir"), raw.get("base_path")
    if not isinstance(chosen, str) or not chosen.strip():
        return str(default)
    if isinstance(saved_base, str) and saved_base and Path(chosen) == Path(saved_base) / "cache":
        return str(default)
    return chosen


def managed_global_settings() -> dict:
    """CORSIA's server-wide oMLX values: what oMLX saved under scheduler, else the shipped default.

    The one source for every writer of these values, the launch flags and the setup POST alike,
    so a value the person saved in oMLX survives a restart, a download and a setup run.
    """
    from llm.local_profiles import OMLX_GLOBAL_SETTINGS
    from llm.managed_omlx_binary import managed_omlx_paths

    try:
        raw = json.loads(managed_omlx_paths().settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    scheduler = raw.get("scheduler", {}) if isinstance(raw, dict) else {}
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    return {key: scheduler.get(key, value) for key, value in OMLX_GLOBAL_SETTINGS.items()}


def managed_launch_settings(settings: dict) -> dict:
    """Return CORSIA's managed server's launch settings, its own folders from the current state root.

    oMLX's command-line folders beat the ones its own settings.json stores, and oMLX
    saves them back into that file, so a state root that moved never leaves the server
    reading or writing an older folder. The SSD cache folder is the person's when they
    chose one in oMLX, and the launch passes it back unchanged. A cache that oMLX's
    settings turn off stays off: passing the cache folder would turn it on.
    """
    from llm.managed_omlx_binary import managed_omlx_paths

    paths = managed_omlx_paths()
    cache = _ssd_cache_folder(paths.settings_path, paths.base_path / "cache")
    folders = {
        "base_path": str(paths.base_path),
        "model_dir": str(paths.model_dir),
        "paged_ssd_cache_dir": None if settings.get("no_cache") else cache,
    }
    # oMLX's CLI flags override its settings file, so pass its saved values back.
    return {**settings, **managed_global_settings(), **folders}
