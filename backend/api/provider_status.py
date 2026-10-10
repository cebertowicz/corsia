"""Provider status payload builders."""

from __future__ import annotations

import json

from fastapi import HTTPException

from api.provider_meta import PROVIDER_META
from infra.db_async import run_in_db
from llm import catalog as model_catalog


def _catalog_row_to_model_view(row: dict, meta: dict) -> dict:
    from llm.context_window import resolve_window_for_row

    return {
        "identifier": row.get("provider_model_id", ""),
        "db_id": row.get("id"),
        "size_bytes": meta.get("size_bytes"),
        "context_length": resolve_window_for_row(row) if row else None,
        "vision": bool(row.get("supports_vision")),
        "tool_use": bool(row.get("supports_tools")),
    }


def _provider_status_payload(name: str) -> dict:
    from llm import network_discovery as _nd
    rows = _nd.get_servers(provider=name)
    any_online = any(r.get("status") == "online" for r in rows)
    server = {
        "running": any_online,
        "servers": [
            {
                "id": r.get("id"),
                "instance_name": r.get("instance_name"),
                "host": r.get("host"),
                "port": r.get("port"),
                "status": r.get("status"),
            }
            for r in rows
        ],
    }

    loaded: list[dict] = []
    disk: list[dict] = []
    for row in model_catalog.list_models(provider=name, include_disabled=True, include_unavailable=False):
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        view = _catalog_row_to_model_view(row, meta)
        disk.append(view)
        if meta.get("state") == "loaded":
            loaded.append(view)

    return {
        "name": name,
        "server": server,
        "loaded_models": loaded,
        "disk_models": disk,
    }


async def build_provider_status(name: str) -> dict:
    if name not in PROVIDER_META:
        raise HTTPException(400, f"Unknown provider: {name}")
    prov_type = PROVIDER_META[name].get("type")
    if prov_type != "network":
        raise HTTPException(400, f"{name} has no server status (type={prov_type})")
    return await run_in_db(_provider_status_payload, name)
