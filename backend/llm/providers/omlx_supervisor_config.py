"""Resolve the configuration of a supervisor-owned oMLX launch."""

from __future__ import annotations

import logging

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
