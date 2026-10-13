"""Discovered model routes — listing, enable/disable."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from infra.auth import require_user
from infra.state.ws import notify_models_changed
from llm import catalog as model_catalog

router = APIRouter(
    prefix="/api/settings/models",
    tags=["discovered_models"],
    # Bearer header or ?token=: agents authenticate by header, never a URL.
    dependencies=[Depends(require_user)],
)


def _settings_inventory() -> list[dict]:
    """Return settings rows with the exact profile window facts runtime resolves."""
    from infra.database import get_db, init_model_profiles
    from llm.context_window import resolve_window_facts_for_rows, saved_overrides_by_catalog_id

    rows = model_catalog.list_models(include_disabled=True, include_unavailable=True)
    init_model_profiles()
    db = get_db()
    try:
        from connections.models import connection_disabled

        facts = resolve_window_facts_for_rows(rows, db=db)
        overrides = saved_overrides_by_catalog_id(rows, db=db)
        disabled_harnesses = {
            harness for harness in ("claude-code", "codex")
            if connection_disabled(harness, db=db)
        }
    finally:
        db.close()
    from llm.model_status import harness_fields, versioned_name

    for row, window in zip(rows, facts):
        model_id = str(row["id"])
        metadata = json.loads(row.get("metadata_json") or "{}")
        row["display_name"] = versioned_name(row, metadata)
        if row.get("harness") in ("claude-code", "codex"):
            row.update(harness_fields(str(row["harness"]), metadata))
            row["provider_enabled"] = row["harness"] not in disabled_harnesses
        row["context_length"] = window.effective
        row["provider_context_length"] = window.discovered
        row["context_window_source"] = window.source
        row["context_window_override"] = overrides.get(model_id)
    return rows


@router.get("")
async def list_all_models_for_settings():
    """Return full model inventory for settings UI (including disabled/unavailable)."""
    return {"models": _settings_inventory()}


# Harness row ids carry their CLI prefix (``codex/gpt-5.6-sol``), so the id is a path.
@router.patch("/{model_id:path}")
async def patch_model(model_id: str, body: dict):
    """Update a model (enable/disable, alias)."""
    m = model_catalog.get_model(model_id)
    if not m:
        raise HTTPException(404, f"Model not found: {model_id}")

    updates = {}
    if "enabled" in body:
        updates["enabled"] = 1 if body["enabled"] else 0
    if "alias" in body:
        alias = body["alias"]
        if alias is not None and not isinstance(alias, str):
            raise HTTPException(400, "alias must be text or null")
        # An empty label clears it, so the discovered name shows again.
        updates["alias"] = (alias or "").strip() or None
    if not updates:
        raise HTTPException(400, "No valid fields to update")

    model_catalog.update_model(model_id, updates)
    notify_models_changed()  # both a hide and a new label change the picker
    return {"ok": True, "model_id": model_id, "updated": list(updates.keys())}
