"""The loaded and available models a network server reports, shaped for the UI."""

from __future__ import annotations

import logging

from infra.db_async import run_in_db
from infra.state.ws import notify_models_changed
from llm import network_discovery as nd
from llm import network_endpoints as ne
from llm.local_sources import corsia_omlx_server
from llm.network_catalog_ids import add_catalog_ids

log = logging.getLogger(__name__)


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
    if model.get("pinned"):
        shaped["pinned"] = True
    if model.get("starting"):
        shaped["starting"] = True
    for key in ("pin_fits", "keep_line"):
        if key in model:
            shaped[key] = model[key]
    if model.get("engine_type"):
        shaped["engine_type"] = model["engine_type"]
        shaped["vision"] = model["engine_type"] == "vlm"
    return shaped


async def _omlx_preloading(mgmt_base: str) -> bool:
    """Whether oMLX is still loading its pinned models at start (``/health`` 503 "loading").

    The port is bound and ``/api/status`` answers "ok" meanwhile, so the server
    already reads online; only ``/health`` says it isn't ready.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{mgmt_base.removesuffix('/api')}/health")
        return response.status_code == 503 and (response.json() or {}).get("status") == "loading"
    except (httpx.HTTPError, ValueError):
        return False


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

    # Only a pinned model that isn't loaded can be on its way, so the health call waits for one.
    waiting = [m for m in canonical_models if m.get("pinned") and not m.get("loaded")]
    if waiting and await _omlx_preloading(mgmt_base):
        # It is on its way: the row says Loading, not "Not loaded".
        for m in waiting:
            m["starting"] = True

    own = await run_in_db(corsia_omlx_server)
    if own is not None and own["id"] == server_id:
        # Keeping a model loaded is CORSIA's own oMLX's alone: a person's oMLX is never pinned.
        from infra.host_resources import annotate_keep_loaded
        from llm.local_keep_loaded import failures

        annotate_keep_loaded(canonical_models, failures(server_id))

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



def notify_cached_model_unloaded(server_id: str, model_id: str) -> None:
    """Optimistically mark a model unloaded and notify clients."""
    nd._LOADED_BY_SERVER.get(server_id, set()).discard(model_id)
    notify_models_changed()
