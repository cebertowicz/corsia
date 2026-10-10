"""Model listing, profiles, model changes."""

from __future__ import annotations

import asyncio

from infra.time import utc_now_iso

from fastapi import APIRouter, Depends, HTTPException, Request

from infra.auth import require_user
from infra.database import get_db, init_model_profiles, write_transaction
from infra.db_async import run_in_db, run_read
from llm import catalog as model_catalog
from llm.capabilities import get_model_capabilities, snapshot_active_threads
from llm.context_window import (
    resolve_unsaved_window_facts,
    resolve_window_facts,
    resolve_window_for_row,
    saved_window_override,
    validate_profile_context_window,
)

router = APIRouter(prefix="/api", tags=["models"])

# Profile fields reported from resolution, never copied from the saved row.
_RESOLVED_PROFILE_FIELDS = frozenset({"context_window", "max_tokens"})


# API and agent tool intentionally share llm.model_catalog_projection.


@router.get("/models")
# FastAPI always injects ``request``; None reaches only in-process callers of the
# unscoped catalog, and authorize_scope refuses a scoped read without it.
async def list_models(refresh_usage: bool = False, request: Request = None,  # type: ignore[assignment]
                      project_id: str | None = None, thread_id: str | None = None,
                      account_id: str | None = None):
    # Status truth is computed in one place (llm.model_status) so the switcher,
    # the catalog, and spawn-safety all read the same source (IP36).
    from llm.model_catalog_projection import model_catalog_projection
    from api.model_account_scope import authorize_scope

    scoped = await run_read(authorize_scope, request, project_id=project_id,
                            thread_id=thread_id, account_id=account_id)
    if refresh_usage:
        from connections.harness_usage_check import schedule_limited_usage_rechecks

        await schedule_limited_usage_rechecks()
    if not scoped:
        from connections.harness_refresh import schedule_stale_refresh

        schedule_stale_refresh()
        return await run_read(model_catalog_projection, snapshot_active_threads())
    return await run_read(model_catalog_projection, snapshot_active_threads(), thread_id=thread_id,
                          project_id=project_id, account_id=account_id)


@router.get("/models/{model_id:path}/profile")
async def get_model_profile(model_id: str):
    """Get engine profile for a model (works for both local and cloud models)."""
    model = model_catalog.get_model(model_id)
    if not model:
        raise HTTPException(404, f"Unknown model: {model_id}")

    caps = get_model_capabilities(model_id)
    # One resolution carries both the counted-against window and which source
    # produced it, so the editor never recomputes the rule.
    windows = resolve_window_facts(model)
    unsaved = resolve_unsaved_window_facts(model)

    # context_window is the resolved window dispatch uses and max_tokens the
    # provider-published output limit; neither is taken from the saved row.
    defaults = {
        "model_id": model_id,
        "context_window": windows.effective,
        "context_window_source": windows.source,
        # What clearing a saved window lands on, so the editor never guesses it.
        "context_window_unsaved": unsaved.effective,
        "context_window_unsaved_source": unsaved.source,
        "max_tokens": model.get("default_max_tokens"),
        "parallel_slots": 1,
        "reasoning_budget": -1,
        "flash_attention": 1,
        "custom_args": None,
        "capabilities": caps,
    }

    init_model_profiles()

    def _load_profile():
        db = get_db()
        row = db.execute("SELECT * FROM model_profiles WHERE model_id = ?", (model_id,)).fetchone()
        db.close()
        return dict(row) if row else None

    row = await run_in_db(_load_profile)

    for k, v in (row or {}).items():
        if v is not None and k in defaults and k not in _RESOLVED_PROFILE_FIELDS:
            defaults[k] = v
    # Only what the user saved, so an editor never presents discovery as a setting.
    defaults["context_window_override"] = saved_window_override(row, model_id)

    return defaults


@router.patch("/models/{model_id:path}/profile", dependencies=[Depends(require_user)])
async def update_model_profile(model_id: str, body: dict):
    """Update engine profile for a model."""
    if not model_catalog.get_model(model_id):
        raise HTTPException(404, f"Unknown model: {model_id}")

    init_model_profiles()
    now = utc_now_iso()
    # No max_tokens: output limits are the provider's, and nothing reads a saved one.
    allowed_fields = {"context_window", "parallel_slots", "reasoning_budget", "flash_attention", "custom_args"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    try:
        validate_profile_context_window(updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    def _save():
        from infra.input_writes import release_checkpoint_inputs

        with write_transaction(None, scope="update_model_profile") as db:
            model = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
            if model is None:
                raise HTTPException(404, f"Unknown model: {model_id}")
            before = resolve_window_for_row(model, db=db)
            existing = db.execute("SELECT model_id FROM model_profiles WHERE model_id = ?", (model_id,)).fetchone()
            if existing:
                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    params = list(updates.values()) + [now, model_id]
                    db.execute(f"UPDATE model_profiles SET {set_clause}, updated_at = ? WHERE model_id = ?", params)
            else:
                u = {**updates, "model_id": model_id, "updated_at": now}
                cols = ", ".join(u)
                placeholders = ", ".join("?" for _ in u)
                db.execute(f"INSERT INTO model_profiles ({cols}) VALUES ({placeholders})", list(u.values()))
            after = resolve_window_for_row(model, db=db)
            return (
                release_checkpoint_inputs(db, model_id)
                if after is not None and after != before else ()
            )

    released = await run_in_db(_save)
    if released:
        from infra.thread_execution import drive_thread

        # Each committed release owes its own wake; one recipient's failure
        # must not suppress or cancel another recipient's recovery.
        results = await asyncio.gather(
            *(drive_thread(thread_id) for thread_id in released),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("model profile recovery failures", errors)
    return {"ok": True, "model_id": model_id, "updated": list(updates.keys())}
