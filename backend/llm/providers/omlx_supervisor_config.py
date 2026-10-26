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
    for key in ("api_key", "model_dir", "max_model_memory"):
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


def _ssd_cache_enabled(settings_path: Path) -> bool:
    """Whether oMLX's own settings leave its SSD cache on (its default when unread)."""
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    cache = raw.get("cache") if isinstance(raw, dict) else None
    return not (isinstance(cache, dict) and cache.get("enabled") is False)


def managed_launch_settings(settings: dict) -> dict:
    """Return CORSIA's managed server's launch settings, every folder from the current state root.

    oMLX's command-line folders beat the ones its own settings.json stores, so a
    state root that moved never leaves the server reading or writing an older
    folder, whatever that file or the server row still names. A cache that
    oMLX's settings turn off stays off: passing the cache folder would turn it on.
    """
    from llm.local_profiles import OMLX_GLOBAL_SETTINGS
    from llm.managed_omlx_binary import managed_omlx_paths

    paths = managed_omlx_paths()
    cache_on = not settings.get("no_cache") and _ssd_cache_enabled(paths.settings_path)
    folders = {
        "base_path": str(paths.base_path),
        "model_dir": str(paths.model_dir),
        "paged_ssd_cache_dir": str(paths.base_path / "cache") if cache_on else None,
    }
    # The global values are launch-time settings too: applying them here updates
    # existing CORSIA-managed installs at their next restart, without writing to
    # a person's oMLX row or server settings.
    return {**settings, **OMLX_GLOBAL_SETTINGS, **folders}
