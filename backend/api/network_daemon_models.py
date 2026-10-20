"""The loaded and available models a network server reports, shaped for the UI."""

from __future__ import annotations

import logging

from infra.db_async import run_in_db
from infra.state.ws import notify_models_changed
from llm import network_discovery as nd
from llm import network_endpoints as ne
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
