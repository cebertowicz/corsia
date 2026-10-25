"""Resolve the configuration of a supervisor-owned oMLX launch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from infra.db_async import run_in_db
from llm.providers.omlx_endpoints import _resolve_endpoint as _resolve_endpoint_via_freshness

log = logging.getLogger("llm.providers.omlx.supervisor")

MOVED_SETTINGS_UNREPOINTED = (
    "Local models didn’t start: CORSIA moved your data but couldn’t update their settings to match. "
    "Choose Start to try again."
)


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


def _names_older_base(settings: dict, base: str) -> bool:
    stored = settings.get("base_path")
    return isinstance(stored, str) and bool(stored) and stored != base


async def managed_launch_settings(
    cfg: dict, settings: dict, reread: Callable[[dict], dict],
) -> tuple[dict, dict[str, Any] | None]:
    """Return CORSIA's managed row's launch settings, or a refusal to start."""
    from llm.local_profiles import OMLX_GLOBAL_SETTINGS
    from llm.managed_omlx_binary import managed_omlx_paths

    # The state root owns where the managed server writes. A row still on an
    # older base means the startup heal could not repoint every oMLX file, and
    # starting there would recreate the old data folder.
    base = str(managed_omlx_paths().base_path)
    if _names_older_base(settings, base):
        from infra.moved_state_heal import heal_managed_omlx

        await run_in_db(heal_managed_omlx)
        settings = await run_in_db(reread, cfg)
    if _names_older_base(settings, base):
        log.warning("start_server: managed oMLX row %s still names an older base; not starting",
                    cfg.get("server_id"))
        return settings, {"ok": False, "message": MOVED_SETTINGS_UNREPOINTED}
    # This is a launch-time setting too: applying it here updates existing
    # CORSIA-managed installs at their next restart, without writing to a
    # person's oMLX row or server settings.
    return {**settings, **OMLX_GLOBAL_SETTINGS, "base_path": base}, None
