from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from infra.database import write_transaction
from llm.local_downloads import settle_listed
from llm.model_metadata import extract_context_length
from llm.omlx_capabilities import apply_metadata, capture_configured, capture_reported

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _looks_like_vlm(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(
        tag in lowered
        for tag in ("-vl-", "-vl2", "vision", "-vlm", "pixtral", "llava", "moondream")
    )


def _parse_omlx_status(response) -> dict[str, dict]:
    if response.status_code != 200:
        raise RuntimeError(f"oMLX status returned HTTP {response.status_code}")
    status_json = response.json()
    models = status_json.get("models") if isinstance(status_json, dict) else None
    if not isinstance(models, list):
        raise ValueError("oMLX /v1/models/status returned an invalid payload")
    canonical: dict[str, dict] = {}
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("oMLX /v1/models/status returned an invalid model")
        provider_model_id = model.get("id")
        if not provider_model_id or model.get("is_hidden"):
            continue
        canonical[provider_model_id] = {
            "id": provider_model_id,
            "loaded": bool(model.get("loaded")),
            "engine_type": (model.get("engine_type") or "").lower(),
            "context_length": extract_context_length(model),
            "size_bytes": int(model.get("estimated_size") or 0) or None,
            "pinned": bool(model.get("pinned")),
        }
        status_alias = (model.get("model_alias") or "").strip()
        if status_alias:
            canonical[provider_model_id]["model_alias"] = status_alias
        capture_reported(model, canonical[provider_model_id])
    return canonical


async def _enrich_runtime_windows(
    client, api_base: str, canonical: dict, headers: dict,
) -> None:
    import httpx

    from llm.network_endpoints import models_probe_url

    try:
        response = await client.get(models_probe_url(api_base), headers=headers)
        if response.status_code != 200:
            return
        payload = response.json() or {}
        entries = payload.get("data") if isinstance(payload, dict) else payload
        alias_index = {
            entry["model_alias"]: pmid
            for pmid, entry in canonical.items()
            if entry.get("model_alias")
        }
        for entry in (entries or []):
            entry_id = entry.get("id")
            pmid = entry_id if entry_id in canonical else alias_index.get(entry_id)
            if not entry_id or pmid is None or canonical[pmid].get("context_length") is not None:
                continue
            runtime_window = extract_context_length(entry)
            if runtime_window is not None:
                canonical[pmid]["context_length"] = runtime_window
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(f"oMLX /v1/models runtime-window fetch failed: {exc}")


async def _fetch_omlx_status_data(client, mgmt_base: str, headers: dict) -> dict:
    import httpx

    try:
        response = await client.get(f"{mgmt_base}/status", headers=headers)
        if response.status_code != 200:
            return {}
        return response.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(f"oMLX /api/status fetch failed: {exc}")
        return {}


def _apply_admin_model(canonical: dict[str, dict], model: dict) -> None:
    provider_model_id = model.get("id")
    if not provider_model_id or provider_model_id not in canonical:
        return
    settings = model.get("settings") or {}
    alias = (settings.get("model_alias") or "").strip()
    if alias:
        canonical[provider_model_id]["model_alias"] = alias
    capture_configured(settings, canonical[provider_model_id])
    capture_reported(model, canonical[provider_model_id])
    if canonical[provider_model_id].get("context_length") is not None:
        return
    window = extract_context_length(model)
    if window is not None:
        canonical[provider_model_id]["context_length"] = window


async def _enrich_admin_aliases(cfg: dict, canonical: dict[str, dict]) -> None:
    import httpx

    from llm.providers.omlx_endpoints import admin_request

    try:
        response = await admin_request(cfg, "GET", "/models", timeout=5)
        if response.status_code == 200:
            data = response.json() or {}
            models = data if isinstance(data, list) else (
                data.get("data") or data.get("models") or []
            )
            for model in models:
                _apply_admin_model(canonical, model)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(f"oMLX /admin/api/models alias enrichment failed: {exc}")


async def _enrich_generation_configs(cfg: dict, canonical: dict[str, dict]) -> None:
    import httpx

    from llm.providers.omlx_endpoints import admin_request

    async def read(provider_model_id: str, model: dict) -> None:
        try:
            response = await admin_request(cfg, "GET", f"/models/{provider_model_id}/generation_config", timeout=5)
            if response.status_code != 200:
                return
            context_length = extract_context_length(response.json() or {})
            if context_length is not None:
                model["context_length"] = context_length
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("oMLX generation_config context enrichment failed for %s: %s", provider_model_id, exc)

    # Reads run together: a long list costs the slowest read, not the sum.
    await asyncio.gather(*(read(i, m) for i, m in canonical.items() if m.get("context_length") is None))


async def _enrich_admin_models(cfg: dict, canonical: dict[str, dict]) -> None:
    """Add admin-only fields; a server with no usable key keeps its public catalog."""
    from llm.providers.omlx_endpoints import OmlxAdminError

    try:
        await _enrich_admin_aliases(cfg, canonical)
        await _enrich_generation_configs(cfg, canonical)
    except OmlxAdminError as exc:
        log.info("oMLX admin enrichment skipped: %s", exc)
    except Exception as exc:  # best-effort: the public catalog stands without it
        log.warning("oMLX admin enrichment failed (%s)", type(exc).__name__)


async def _fetch_omlx_canonical_models(
    api_base: str,
    mgmt_base: str,
    api_key: str | None,
    server_id: str | None = None,
) -> tuple[list[dict], dict]:
    import httpx

    from llm.providers.omlx_public_auth import public_headers
    headers = await public_headers({"server_id": server_id, "api_key": api_key}, url=api_base)
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            response = await client.get(f"{api_base}/models/status", headers=headers)
            canonical = _parse_omlx_status(response)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            log.warning(f"oMLX /v1/models/status fetch failed: {exc}")
            raise RuntimeError(
                f"oMLX authoritative model discovery failed: {exc}"
            ) from exc
        await _enrich_runtime_windows(client, api_base, canonical, headers)
        status_data = await _fetch_omlx_status_data(client, mgmt_base, headers)
    if api_key or server_id:
        # The endpoint the caller already resolved, so admin calls go where /v1 went.
        parsed = urlsplit(mgmt_base)
        cfg = {"server_id": server_id, "api_key": api_key, "host": parsed.hostname, "port": parsed.port}
        await _enrich_admin_models(cfg, canonical)

    return list(canonical.values()), status_data


def _network_discovery_config(
    server_id: str,
) -> tuple[dict | None, str | None, str | None]:
    from llm.network_discovery import get_server
    from llm.network_endpoints import build_api_base, build_mgmt_base

    server = get_server(server_id)
    if not server or server.get("status") != "online":
        return server, None, None
    return server, build_api_base(server_id), build_mgmt_base(server_id)


def _network_model_payload(
    server_id: str,
    server: dict,
    provider: str,
    model: dict,
    status_data: dict,
    reasoning_shape: dict | None,
) -> dict:
    provider_model_id = model["id"]
    model_alias = model.get("model_alias")
    pmid_lower = provider_model_id.lower()
    description = (
        "MLX 4-bit" if "-4bit" in pmid_lower
        else "MLX 8-bit" if "-8bit" in pmid_lower
        else "MLX"
    )
    metadata: dict = {
        "server_id": server_id,
        "server_name": server.get("instance_name", ""),
        "device": status_data.get("device", ""),
    }
    if model.get("size_bytes"):
        metadata["size_bytes"] = model["size_bytes"]
    if model_alias:
        metadata["model_alias"] = model_alias
    apply_metadata(model, reasoning_shape, metadata)
    engine = model.get("engine_type") or ""
    supports_vision = (
        1 if engine == "vlm" else 0
        if engine else 1 if _looks_like_vlm(provider_model_id) else 0
    )
    return {
        "provider_model_id": provider_model_id,
        "litellm_model": f"{provider}@{server_id}/{provider_model_id}",
        "display_name": model_alias or provider_model_id.split("/")[-1],
        "description": description,
        "context_window": model.get("context_length"),
        "supports_vision": supports_vision,
        "metadata_json": json.dumps(metadata),
    }


def _upsert_network_model(db, provider: str, payload: dict, now: str) -> str:
    existing = db.execute(
        "SELECT id FROM models WHERE provider = ? AND provider_model_id = ?",
        (provider, payload["provider_model_id"]),
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE models SET available = 1, litellm_model = ?,
               display_name = ?, description = ?,
               context_window = COALESCE(?, context_window), supports_vision = ?,
               metadata_json = ?, last_seen_at = ?, updated_at = ? WHERE id = ?""",
            (payload["litellm_model"], payload["display_name"],
             payload["description"], payload["context_window"],
             payload["supports_vision"], payload["metadata_json"], now, now,
             existing["id"]),
        )
        return "updated"

    # Unknown sampling/output defaults stay NULL; discovery measures only the
    # runtime window and capabilities, so caller-owned budgets remain authoritative.
    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            description, backend, source, enabled, available, context_window,
            supports_vision, supports_tools, default_temperature, default_max_tokens,
            metadata_json, discovered_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,1,1,?,?,1,NULL,NULL,?,?,?,?,?)""",
        ("vmlx-" + str(uuid.uuid4())[:8], provider,
         payload["provider_model_id"], payload["litellm_model"],
         payload["display_name"], payload["description"], "network", "discovered",
         payload["context_window"], payload["supports_vision"],
         payload["metadata_json"], now, now, now, now),
    )
    return "discovered"


def _reconcile_stale_network_models(
    db,
    server_id: str,
    provider: str,
    seen_pmids: set[str],
    now: str,
) -> int:
    from infra.settings import get_host_access_reviewer_model

    stale_rows = db.execute(
        "SELECT id, provider_model_id, litellm_model, source FROM models "
        "WHERE provider = ? AND litellm_model LIKE ?",
        (provider, f"{provider}@{server_id}/%"),
    ).fetchall()
    configured_reviewer = get_host_access_reviewer_model(db=db)
    missing = 0
    for row in stale_rows:
        if row["provider_model_id"] in seen_pmids:
            continue
        if row["source"] == "discovered" and row["id"] != configured_reviewer:
            db.execute("DELETE FROM models WHERE id = ?", (row["id"],))
        else:
            db.execute(
                "UPDATE models SET available = 0, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        missing += 1
    return missing


def _reconcile_network_models(
    server_id: str,
    server: dict,
    canonical_models: list[dict],
    status_data: dict,
    now: str,
    result: dict,
) -> dict:
    """Apply one authoritative oMLX snapshot in the DB worker pool."""
    from llm.local_profiles import managed_reasoning_effort
    from llm.reasoning_config import reasoning_shape_from_discovery

    provider = server.get("provider", "omlx")
    managed_effort = managed_reasoning_effort(server)
    reasoning_by_pmid = {model["id"]: reasoning_shape_from_discovery(model, managed_effort=managed_effort)
                         for model in canonical_models}
    seen_pmids = {model["id"] for model in canonical_models}
    with write_transaction(None, scope="reconcile_network_models") as db:
        for model in canonical_models:
            payload = _network_model_payload(
                server_id, server, provider, model, status_data,
                reasoning_by_pmid[model["id"]],
            )
            result[_upsert_network_model(db, provider, payload, now)] += 1
        result["missing"] = _reconcile_stale_network_models(
            db, server_id, provider, seen_pmids, now,
        )

    settle_listed(server_id, seen_pmids)
    result["ok"] = True
    return result


async def discover_network_models(server_id: str) -> dict:
    from infra.db_async import run_in_db

    result: dict = {"ok": False, "discovered": 0, "updated": 0, "missing": 0}

    server, api_base, mgmt_base = await run_in_db(
        _network_discovery_config, server_id,
    )

    if not server:
        result["error"] = f"Server not found: {server_id}"
        return result

    if server.get("status") != "online":
        result["error"] = "Server is offline"
        return result

    if not api_base or not mgmt_base:
        result["error"] = "Server has no reachable endpoint"
        return result

    try:
        canonical_models, status_data = await _fetch_omlx_canonical_models(
            api_base, mgmt_base, None, server_id,
        )
    except Exception as exc:
        result["error"] = f"Connection failed: {exc}"
        return result


    try:
        return await run_in_db(
            _reconcile_network_models,
            server_id,
            server,
            canonical_models,
            status_data,
            _now(),
            result,
        )
    except Exception as exc:
        log.error("network model discovery DB error: %s", exc)
        result["error"] = str(exc)
        return result
