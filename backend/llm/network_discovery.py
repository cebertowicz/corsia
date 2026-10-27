"""Server CRUD and health sweep for oMLX network providers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from infra.db_async import run_in_db
from infra.database import get_db, write_transaction
from infra.key_store_servers import remove_server_key
from llm.network_endpoints import (
    get_endpoints,
    is_cloud_external,
    upsert_endpoint,
)
from llm.server_observation import loaded_token, record_discovery, record_reachable, record_unreachable

log = logging.getLogger(__name__)

HEALTH_SWEEP_INTERVAL = 10

_LOADED_BY_SERVER: dict[str, set[str]] = {}

def notify_cached_model_loaded(server_id: str, model_id: str, token: object) -> None:
    """Publish a load only if the observation it started under is still current."""
    from infra.state.ws import notify_models_changed
    if loaded_token(server_id) is not token:
        return
    _LOADED_BY_SERVER[server_id] = get_loaded_model_ids(server_id) | {model_id}
    notify_models_changed()


def get_loaded_model_ids(server_id: str) -> set[str]:
    """Return model ids currently loaded on a swept online server."""
    return set(_LOADED_BY_SERVER.get(server_id, ()))

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def upsert_server(
    provider: str,
    instance_id: str,
    instance_name: str,
    host: str,
    port: int,
    status: str,
    *,
    kind: str = "managed",
    dialect: str | None = None,
    source: str = "manual",
    machine_id: str | None = None,
    db=None,
) -> str:
    """Register or update a server row and its latest endpoint.

    ``status`` is REQUIRED — the caller must pass the real probed status so a
    reachable server is never silently registered as offline (a caller that just
    verified reachability passes ``"online"``). ``kind`` ∈ {``managed``,
    ``external``}; ``dialect`` ∈ {``openai``, ``anthropic``} for external rows
    (NULL for managed). ``metadata_json`` (capability overrides) is
    owned by :func:`set_server_metadata` and is never written here — so a re-add
    can't wipe a stored token.
    """
    with write_transaction(db, scope="upsert_network_server") as connection:
        now = _now_iso()
        row = connection.execute(
            "SELECT id FROM network_servers WHERE provider = ? AND instance_id = ?",
            (provider, instance_id),
        ).fetchone()
        if row:
            sid = row["id"]
            sets = ["instance_name = ?", "port = ?", "status = ?", "kind = ?",
                    "dialect = ?", "last_seen_at = ?", "updated_at = ?"]
            vals: list = [instance_name, port, status, kind, dialect, now, now]
            if machine_id:
                sets.append("machine_id = ?")
                vals.append(machine_id)
            vals.append(sid)
            connection.execute(f"UPDATE network_servers SET {', '.join(sets)} WHERE id = ?", vals)
        else:
            sid = f"ns-{uuid.uuid4().hex[:10]}"
            connection.execute(
                """INSERT INTO network_servers
                       (id, provider, kind, dialect, instance_id, machine_id,
                        instance_name, port, status, source,
                        last_seen_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, provider, kind, dialect, instance_id, machine_id,
                 instance_name, port, status, source, now, now, now),
            )
            log.info("Registered %s (%s) as %s [kind=%s, dialect=%s, source=%s]",
                     instance_name, instance_id, sid, kind, dialect, source)
        upsert_endpoint(sid, host, ok=(status == "online"), db=connection)
    return sid

def get_server(server_id: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM network_servers WHERE id = ?", (server_id,)).fetchone()
    return dict(row) if row else None

def set_server_metadata(
    server_id: str, *, db=None, clear: tuple[str, ...] = (), **fields: Any,
) -> None:
    """Merge ``fields`` into a server's ``metadata_json`` blob, removing ``clear`` keys."""
    with write_transaction(db, scope="set_server_metadata") as connection:
        row = connection.execute(
            "SELECT metadata_json FROM network_servers WHERE id = ?", (server_id,),
        ).fetchone()
        if not row:
            return
        existing = parse_server_metadata(server_id, row["metadata_json"]) or {}
        for key in clear:
            existing.pop(key, None)
        existing.update({k: v for k, v in fields.items() if v is not None})
        connection.execute(
            "UPDATE network_servers SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(existing), _now_iso(), server_id),
        )

def get_server_metadata(server_id: str) -> dict:
    """Return the parsed ``metadata_json`` for a server, or ``{}``."""
    return read_server_metadata(server_id) or {}

def read_server_metadata(server_id: str) -> dict | None:
    """``{}`` when no metadata is stored; None when unreadable, which endpoint
    resolution must not mistake for "no base stored": it may have held https."""
    db = get_db()
    row = db.execute(
        "SELECT metadata_json FROM network_servers WHERE id = ?", (server_id,),
    ).fetchone()
    return parse_server_metadata(server_id, row["metadata_json"] if row else None)

def parse_server_metadata(server_id: str, raw: str | None) -> dict | None:
    """The one parse of a ``metadata_json`` blob, for callers holding the raw column.

    ``{}`` when nothing is stored; None, logged, when it cannot be read, so no
    reader mistakes corruption for an endpoint that declared nothing.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        log.warning(f"server metadata_json malformed for {server_id}: {e}")
        return None
    if not isinstance(parsed, dict):
        log.warning(f"server metadata_json for {server_id} is not an object")
        return None
    return parsed

def get_servers(provider: str | None = None, status: str | None = None) -> list[dict]:
    """List network-provider servers, optionally filtered by provider/status."""
    db = get_db()
    where = []
    args: list = []
    if provider is not None:
        where.append("provider = ?")
        args.append(provider)
    if status is not None:
        where.append("status = ?")
        args.append(status)
    else:
        where.append("status != 'removed'")
    sql = "SELECT * FROM network_servers WHERE " + " AND ".join(where) + " ORDER BY instance_name"
    rows = db.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def update_server_status(server_id: str, *, db=None, **kwargs: Any) -> None:
    with write_transaction(db, scope="update_server_status") as connection:
        sets = ["updated_at = ?"]
        vals: list = [_now_iso()]
        for key in (
            "status", "latency_ms", "memory_total_mb", "memory_used_mb", "gpu_info",
            "machine_id",
        ):
            if key in kwargs and (key != "machine_id" or kwargs[key] is not None):
                sets.append(f"{key} = ?")
                vals.append(kwargs[key])
        vals.append(server_id)
        # A sweep that read the server before it was removed or turned off must
        # not revive it.
        connection.execute(
            f"UPDATE network_servers SET {', '.join(sets)} "
            "WHERE id = ? AND status NOT IN ('removed', 'disabled')",
            vals,
        )


def remove_server(server_id: str) -> bool:
    """Retire a server and withdraw its models from the catalog in one write."""
    with write_transaction(None, scope="remove_network_server") as db:
        row = db.execute(
            "SELECT provider FROM network_servers WHERE id = ?", (server_id,),
        ).fetchone()
        if not row:
            return False
        db.execute(
            "UPDATE network_servers SET status = 'removed', updated_at = ? WHERE id = ?",
            (_now_iso(), server_id),
        )
        _mark_server_models_unavailable(server_id, row["provider"], db=db)
        remove_server_key(server_id, db=db)
    _LOADED_BY_SERVER.pop(server_id, None)
    return True


def set_server_enabled(server_id: str, enabled: bool) -> bool:
    """Turn a server off, withdrawing its models, or back on for its next probe."""
    with write_transaction(None, scope="set_network_server_enabled") as db:
        row = db.execute(
            "SELECT provider, status FROM network_servers WHERE id = ? AND status != 'removed'",
            (server_id,),
        ).fetchone()
        if not row:
            return False
        if not enabled:
            db.execute(
                "UPDATE network_servers SET status = 'disabled', updated_at = ? WHERE id = ?",
                (_now_iso(), server_id),
            )
            _mark_server_models_unavailable(server_id, row["provider"], db=db)
        elif row["status"] == "disabled":
            # Offline until probed: the probe that follows decides online.
            db.execute(
                "UPDATE network_servers SET status = 'offline', updated_at = ? WHERE id = ?",
                (_now_iso(), server_id),
            )
    if not enabled:
        _LOADED_BY_SERVER.pop(server_id, None)
    return True


async def health_sweep_loop() -> None:
    while True:
        try:
            await _sweep_all_servers()
        except Exception as e:
            log.warning("Health sweep error: %s", e)
        await asyncio.sleep(HEALTH_SWEEP_INTERVAL)


async def _sweep_all_servers() -> None:
    servers = await run_in_db(get_servers)
    if not servers:
        return
    async with httpx.AsyncClient(timeout=5.0) as client:
        for server in servers:
            if server.get("status") != "disabled":
                await _sweep_one(client, server)


async def _sweep_one(client: httpx.AsyncClient, server: dict) -> None:
    """Probe a server and update its status, branching by provider kind.

    Managed (oMLX) probes the rich ``/api/status`` surface; LAN External probes
    the generic ``/v1/models`` (observe-only — listed models are what the user
    has running, CORSIA never load/unloads them). A cloud External server is
    never probed: discovery and real dispatches observe it
    (``llm.server_observation``)."""
    if server.get("kind") == "external":
        if not await run_in_db(is_cloud_external, server):
            await _sweep_external(client, server)
    else:
        await _sweep_managed(client, server)


async def _sweep_external(client: httpx.AsyncClient, server: dict) -> None:
    """Observe-only health probe for an External provider via ``/v1/models``."""
    from llm.external_sweep import sweep_external

    await sweep_external(client, server)


async def _sweep_managed(client: httpx.AsyncClient, server: dict) -> None:
    """Ping known endpoints and aggregate the managed (oMLX) server status."""
    from llm.providers.omlx_public_probe import probe_registered_status

    sid = server["id"]
    token = loaded_token(sid)
    port = server["port"]
    endpoints = await run_in_db(get_endpoints, sid)

    any_ok = False
    fastest_latency: int | None = None
    latest_data: dict | None = None

    for endpoint in endpoints:
        result = await probe_registered_status(client, sid, endpoint["host"], port)
        if result is None:
            continue
        host, latency_ms, latest_data = result
        await run_in_db(
            upsert_endpoint, sid, host, ok=True, latency_ms=latency_ms,
        )
        any_ok = True
        if fastest_latency is None or latency_ms < fastest_latency:
            fastest_latency = latency_ms

    if any_ok and latest_data is not None:
        loaded = latest_data.get("loaded_models") or []
        await record_reachable(
            server,
            loaded={m for m in loaded if isinstance(m, str) and m},
            token=token,
            latency_ms=fastest_latency,
            memory_total_mb=int(latest_data.get("memory_total_gb", 0) * 1e9 / (1024 ** 2)),
            memory_used_mb=int(latest_data.get("memory_used_gb", 0) * 1e9 / (1024 ** 2)),
            gpu_info=latest_data.get("device"),
            machine_id=latest_data.get("machine_id"),
        )
    else:
        await record_unreachable(server)


def _record_server_offline(server: dict, provider: str) -> None:
    """Commit the offline server and its model availability as one DB-only result."""
    with write_transaction(None, scope="record_server_offline") as db:
        update_server_status(server["id"], status="offline", db=db)
        if server.get("status") != "offline":
            _mark_server_models_unavailable(server["id"], provider, db=db)


def _mark_server_models_unavailable(server_id: str, provider: str, *, db=None) -> None:
    with write_transaction(db, scope="mark_server_models_unavailable") as connection:
        connection.execute(
            "UPDATE models SET available = 0, updated_at = ? WHERE litellm_model LIKE ?",
            (_now_iso(), f"{provider}@{server_id}/%"),
        )

_DISCOVERY_LOCKS: dict[str, asyncio.Lock] = {}


async def _discover_and_notify(server_id: str) -> dict:
    """Return the core discovery receipt; enrichment never replaces that receipt."""
    lock = _DISCOVERY_LOCKS.setdefault(server_id, asyncio.Lock())
    if lock.locked():
        return {"ok": False, "error": "Model discovery is already running"}
    async with lock:
        server = await run_in_db(get_server, server_id)
        is_external = bool(server and server.get("kind") == "external")
        try:
            if is_external:
                from llm.external_provider import discover_external_models
                result = await discover_external_models(server_id)
            else:
                from llm.catalog import discover_network_models
                result = await discover_network_models(server_id)
        except Exception as e:
            result = {"ok": False, "error": str(e)}
            if is_external:
                log.warning("discover_and_notify(%s) failed: %s", server_id, e)
            else:
                record_discovery(server_id, result)
            return result
        if is_external or record_discovery(server_id, result):
            _notify_models_changed_safe()
        if not result.get("ok"):
            return result
        if is_external:
            # External is observe-only: no KV-cache to warm. Models never
            # checked get their one image probe, then the selector refreshes.
            from llm.external_image_input import detect_image_input

            try:
                if await detect_image_input(server_id):
                    _notify_models_changed_safe()
            except Exception as e:
                log.warning("external image probe (%s) failed: %s", server_id, e)
            return result
        try:
            from llm.network_endpoints import build_api_base
            api_base = await run_in_db(build_api_base, server_id)
            if api_base:
                from llm.providers.omlx_introspection import warm_cache
                await warm_cache(api_base, server_id=server_id)
        except Exception as e:
            log.warning("oMLX introspection warm for %s failed: %s", server_id, e)
        return result


async def discover_and_notify(server_id: str) -> dict:
    """Public single-server discovery entry point, returning its core receipt."""
    return await _discover_and_notify(server_id)


def _notify_models_changed_safe() -> None:
    """Notify model-list subscribers without failing discovery callers."""
    try:
        from infra.state.ws import notify_models_changed
        notify_models_changed()
    except Exception as e:
        log.warning("notify_models_changed failed: %s", e)
