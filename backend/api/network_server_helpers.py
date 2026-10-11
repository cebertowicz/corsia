"""Helpers for network server management routes."""

from __future__ import annotations

import asyncio
import logging
import httpx
from fastapi import HTTPException

from infra.db_async import run_in_db
from infra.database import get_db, write_transaction
from infra.key_store import KeyStoreError
from infra.key_store_servers import (
    clean_key, remove_server_key, resolve_server, set_server_key,
)
from infra.state.ws import notify_models_changed
from llm import network_discovery as nd
from llm import network_endpoints as ne
from llm.external_declared_models import (
    LIST_UNSUPPORTED, META_KEY, clean_model_ids, declared_model_ids, declared_model_refusal,
)
from llm.network_catalog_ids import add_catalog_ids
from llm.providers.omlx_endpoints import health_url as omlx_health_url
from llm.providers.omlx_public_auth import public_headers

log = logging.getLogger(__name__)


def _entered_key(raw: str | None) -> str:
    """The key typed in a request, cleaned, or a named 400; empty when none was entered."""
    if not (raw or "").strip():
        return ""
    try:
        return clean_key(raw)
    except KeyStoreError as exc:
        raise HTTPException(400, detail=str(exc)) from None


async def _add_omlx_server(body) -> dict:
    """Probe oMLX and register on success. The key, if any, goes to the key store."""
    key = _entered_key(body.api_key)
    # oMLX owns its health surface; the catalog probe comes from the
    # same owner that answers the call, so add-flow and sweep agree.
    health_url = omlx_health_url(body.hostname, body.port)
    catalog_url = ne.models_probe_url(ne.lan_base(body.hostname, body.port))
    try:
        candidate_cfg = {"api_key": key}
        health_headers = await public_headers(candidate_cfg, url=health_url)
        catalog_headers = await public_headers(candidate_cfg, url=catalog_url)
        async with httpx.AsyncClient(timeout=5.0) as client:
            health = await client.get(health_url, headers=health_headers)
            if health.status_code != 200:
                raise HTTPException(400, detail=f"{health_url} returned HTTP {health.status_code}")
            models_resp = await client.get(catalog_url, headers=catalog_headers)
            if models_resp.status_code != 200:
                raise HTTPException(
                    400, detail=f"{catalog_url} returned HTTP {models_resp.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=f"Could not reach {body.hostname}:{body.port} — {e}")

    def _persist_registration():
        with write_transaction(None, scope="register_network_server") as db:
            sid = nd.upsert_server(
                provider="omlx",
                instance_id=f"{body.hostname}:{body.port}",
                instance_name=body.name,
                host=body.hostname,
                port=body.port,
                source="manual",
                status="online",
                db=db,
            )
            if key:
                set_server_key(sid, key, db=db)
        return sid

    sid = await run_in_db(_persist_registration)
    asyncio.create_task(nd.discover_and_notify(sid))
    return {"ok": True, "server_id": sid, "status": "online"}



def _validated_external_request(body) -> tuple[str, str]:
    """The base URL and dialect of an add request, or a named 400."""
    base = ne.validated_base_url((body.base_url or "").strip())
    if not base:
        raise HTTPException(
            400,
            detail="base_url must be a full http(s) URL with a host, and must not "
                   "carry credentials, a query string, or a fragment",
        )
    dialect = (body.dialect or "openai").strip().lower()
    if dialect not in ("openai", "anthropic"):
        raise HTTPException(400, detail=f"Unsupported dialect: {dialect!r}")
    return base, dialect


def _declared_ids(body, dialect: str, action: str) -> list[str] | None:
    """The model ids the request declares, or a named 400 for one that is not an id."""
    try:
        return clean_model_ids(body.model_ids)
    except ValueError as e:
        raise _refuse_endpoint(dialect, "bad_model_ids", str(e), action=action)


def _external_identity(base: str) -> tuple[str, int, str]:
    """Host, port and instance id of one validated External base URL.

    Add and edit both derive identity here, so an edited endpoint is keyed exactly
    as the add path would have keyed the same URL.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return str(parts.hostname), port, f"{parts.hostname}:{port}{parts.path}"


def _origin(base: str | None) -> tuple[str, str, int] | None:
    """Scheme, host and port: the origin a stored key was entrusted to."""
    from urllib.parse import urlsplit

    if not base:
        return None
    host, port, _instance = _external_identity(base)
    return urlsplit(base).scheme, host, port


def _refuse_endpoint(
    dialect: str, reason: str, detail: str, *, action: str = "register",
) -> HTTPException:
    """A named 400, recorded so the failure classes users hit are measurable."""
    from infra.product_events import emit_product_event

    emit_product_event(
        "external_endpoint", f"{action}.failed",
        properties={"dialect": dialect, "reason": reason},
    )
    return HTTPException(400, detail=detail)


async def _probe_external(
    base: str, dialect: str, headers: dict, *, action: str = "register",
    declared: list[str] | None = None,
) -> list:
    """The model list at ``base``, or a 400 naming the URL and the failure."""
    probe_url = ne.models_probe_url(base, dialect=dialect)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(probe_url, headers=headers)
    except Exception as e:
        # Name the failure class: a DNS miss, a TLS refusal and a timeout all look
        # identical to a user that just sees "offline" with no reason attached.
        raise _refuse_endpoint(
            dialect, type(e).__name__,
            f"Could not reach {probe_url} — {type(e).__name__}: {e}", action=action,
        )
    if declared and resp.status_code in LIST_UNSUPPORTED:
        # No list call, or a mistyped path: only a declared model can tell them apart.
        refused = await declared_model_refusal(base, dialect, headers, declared[0])
        if refused:
            raise _refuse_endpoint(dialect, "declared_model_refused", refused, action=action)
        return []
    if resp.status_code != 200:
        detail = f"{probe_url} returned HTTP {resp.status_code}"
        if resp.status_code == 404 and dialect == "openai":
            # The typed URL is used as-is, so a missing version segment is the
            # usual cause; the other is a provider with no list call at all.
            detail += (" — use the base URL your provider documents for OpenAI SDKs, including its "
                       "version path (often /v1), or enter Model IDs if it lists none.")
        raise _refuse_endpoint(dialect, f"http_{resp.status_code}", detail, action=action)
    try:
        payload = resp.json()
        return list(payload.get("data") or [])
    except (ValueError, AttributeError, TypeError):
        # Reached, but the answer is not a model list: say that, not "unreachable".
        raise _refuse_endpoint(
            dialect, "unexpected_body",
            f"{probe_url} answered HTTP 200, but not with a model list", action=action,
        )


async def update_external_server(server_id: str, body) -> dict:
    """Validate, probe, then atomically update one External endpoint in place.

    The stable server id is deliberately retained so existing model selections and
    dispatch selectors remain valid. An omitted key (including whitespace-only
    input) keeps the currently stored key, while a supplied key replaces it. A
    stored key is only ever sent to the origin it was saved for: moving the
    endpoint to another scheme, host or port requires the key to be entered again.
    """
    from infra.product_events import emit_product_event
    from llm.external_provider import auth_headers

    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    if server.get("kind") != "external":
        raise HTTPException(400, detail="Only External servers can be edited here")

    base, dialect = _validated_external_request(body)
    stored = await run_in_db(nd.get_server_metadata, server_id)
    supplied_key = _entered_key(body.api_key)
    stored_key = await run_in_db(resolve_server, server_id)
    api_key = supplied_key or stored_key
    moved = _origin(stored.get("base_url")) != _origin(base)
    if moved and not supplied_key and stored_key:
        raise _refuse_endpoint(
            dialect, "key_required_for_new_host",
            "Enter the API key for the new host. A saved key is only sent to "
            "the host it was saved for.", action="edit",
        )
    declared = _declared_ids(body, dialect, "edit")
    host, port, instance_id = _external_identity(base)
    observed = await _probe_external(
        base, dialect, auth_headers(dialect, api_key), action="edit",
        declared=declared if declared is not None else declared_model_ids(stored),
    )

    def _persist_update() -> None:
        with write_transaction(None, scope="update_external_server") as db:
            duplicate = db.execute(
                "SELECT id FROM network_servers WHERE provider = 'external' "
                "AND instance_id = ? AND id != ?",
                (instance_id, server_id),
            ).fetchone()
            if duplicate:
                raise HTTPException(400, detail="Another External server already uses this URL")
            now = nd._now_iso()
            db.execute(
                """UPDATE network_servers
                   SET instance_id = ?, port = ?, dialect = ?,
                       status = CASE status WHEN 'disabled' THEN 'disabled' ELSE 'online' END,
                       last_seen_at = ?, updated_at = ? WHERE id = ?""",
                (instance_id, port, dialect, now, now, server_id),
            )
            db.execute("DELETE FROM network_server_endpoints WHERE server_id = ?", (server_id,))
            ne.upsert_endpoint(server_id, host, ok=True, db=db)
            fields: dict = {"base_url": base}
            if supplied_key:
                set_server_key(server_id, supplied_key, db=db)
            if body.cache_warm_seconds is not None:
                fields["cache_warm_seconds"] = int(body.cache_warm_seconds)
            if body.agent_use_restriction is not None:
                fields["agent_use_restriction"] = body.agent_use_restriction
            if declared:
                fields[META_KEY] = declared
            cleared = (META_KEY,) if declared == [] else ()
            nd.set_server_metadata(server_id, db=db, clear=cleared, **fields)

    await run_in_db(_persist_update)
    asyncio.create_task(nd.discover_and_notify(server_id))
    emit_product_event(
        "external_endpoint", "edited",
        properties={"dialect": dialect, "models": len(observed), "moved": moved,
                    "key_replaced": bool(supplied_key)},
    )
    return {"ok": True, "server_id": server_id, "status": "online", "models": len(observed)}


async def add_external_server(body) -> dict:
    """Register a user-owned cloud endpoint: probe first, persist only on success.

    The URL the user typed is stored verbatim in server metadata, because it
    carries the scheme AND the path — a workspace base ends in
    ``/compatible-mode/v1``, and neither half is recoverable from host+port. The
    probe runs against that same base, so a server that registers here is one we
    actually reached on the wire rather than one we assumed would work.
    """
    from llm.external_provider import auth_headers

    base, dialect = _validated_external_request(body)
    declared = _declared_ids(body, dialect, "register") or []
    host, port, instance_id = _external_identity(base)
    key = _entered_key(body.api_key)
    headers = auth_headers(dialect, key)
    observed = await _probe_external(base, dialect, headers, declared=declared)

    def _persist_registration() -> str:
        with write_transaction(None, scope="register_external_server") as db:
            sid = nd.upsert_server(
                provider="external",
                instance_id=instance_id,
                instance_name=body.name,
                host=host,
                port=port,
                status="online",
                kind="external",
                dialect=dialect,
                source="manual",
                db=db,
            )
            # A registration is the user's whole statement: an empty window or key on a
            # re-add is cleared. An omitted agent-use restriction is kept (no form sends it).
            fields: dict = {"base_url": base}
            if body.cache_warm_seconds:
                fields["cache_warm_seconds"] = int(body.cache_warm_seconds)
            if body.agent_use_restriction is not None:
                fields["agent_use_restriction"] = body.agent_use_restriction
            if key:
                set_server_key(sid, key, db=db)
            else:
                remove_server_key(sid, db=db)
            if declared:
                fields[META_KEY] = declared
            owned = ("cache_warm_seconds", META_KEY)
            nd.set_server_metadata(
                sid, db=db, clear=tuple(k for k in owned if k not in fields), **fields,
            )
        return sid

    sid = await run_in_db(_persist_registration)
    asyncio.create_task(nd.discover_and_notify(sid))
    from infra.product_events import emit_product_event

    emit_product_event(
        "external_endpoint", "registered",
        properties={
            "dialect": dialect, "models": len(observed),
            "declared_cache_window": bool(body.cache_warm_seconds),
            "declared_models": len(declared),
        },
    )
    return {"ok": True, "server_id": sid, "status": "online", "models": len(observed)}


def shape_canonical_model_for_ui(model: dict) -> dict:
    """Translate a canonical model dict into the frontend model shape."""
    pmid = model["id"]
    alias = model.get("model_alias")
    shaped: dict = {
        "id": pmid,
        "model_id": pmid,
        "repo_id": pmid,
        "display_name": alias or pmid,
    }
    if alias:
        shaped["model_alias"] = alias
    if model.get("context_length") is not None:
        shaped["context_length"] = model["context_length"]
    if model.get("size_bytes") is not None:
        shaped["size_bytes"] = model["size_bytes"]
    if model.get("engine_type"):
        shaped["engine_type"] = model["engine_type"]
        shaped["vision"] = model["engine_type"] == "vlm"
    return shaped


async def list_daemon_models(server_id: str, server: dict) -> dict:
    """Return loaded and available daemon models for the UI."""
    if server.get("kind") == "external":
        # No management API to ask: the catalog rows discovery wrote ARE the list.
        from llm.external_catalog_view import external_catalog_models

        return await run_in_db(external_catalog_models, server_id)
    mgmt_base, api_base = await run_in_db(
        lambda: (ne.build_mgmt_base(server_id), ne.build_api_base(server_id)),
    )
    if not mgmt_base or not api_base:
        return {"loaded": [], "available": []}

    from llm.catalog_network_models import _fetch_omlx_canonical_models

    try:
        canonical_models, _ = await _fetch_omlx_canonical_models(
            api_base, mgmt_base, None, server_id,
        )
    except Exception as exc:
        log.debug("list_models canonical fetch failed: %s", exc)
        canonical_models = []

    loaded = [
        shape_canonical_model_for_ui(m)
        for m in canonical_models
        if m.get("loaded")
    ]
    available = [
        shape_canonical_model_for_ui(m)
        for m in canonical_models
        if not m.get("loaded")
    ]
    provider = server.get("provider", "")
    await run_in_db(add_catalog_ids, loaded, provider)
    await run_in_db(add_catalog_ids, available, provider)
    return {"loaded": loaded, "available": available}


def notify_cached_model_loaded(server_id: str, model_id: str) -> None:
    """Optimistically mark a model loaded and notify clients."""
    nd._LOADED_BY_SERVER.setdefault(server_id, set()).add(model_id)
    notify_models_changed()


def notify_cached_model_unloaded(server_id: str, model_id: str) -> None:
    """Optimistically mark a model unloaded and notify clients."""
    nd._LOADED_BY_SERVER.get(server_id, set()).discard(model_id)
    notify_models_changed()
