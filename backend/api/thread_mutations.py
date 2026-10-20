"""Session metadata, model-selection, and destructive-deletion routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api.thread_deletion import delete_owned_thread as _delete_owned_thread
from infra.auth import authenticate, require_human_user
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from api.locality_refusal import locality_refused_response
from infra.local_only import (
    LocalityRefused,
    family_local_only_payload,
    local_only_sources,
    require_local_only_model,
    write_family_local_only,
)
from infra.passive_changes import append_agent_roster_transition
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from infra.time import utc_now_iso
from llm.catalog import dispatch_form_error, is_dispatchable_model
from plans import public
from thread import runtime_boundary
from thread.model import switch_model
from thread.model_state import (
    ModelSwitchConflict,
    ModelSwitchOutcome,
    ModelSwitchPreparationError,
    ThreadQuiescenceError,
)

router = APIRouter(prefix="/api", tags=["threads"])

_METADATA_MUTATIONS = {
    "archived",
    "pinned",
    "project_id",
    "reasoning_effort",
    "title",
}


def _owned_thread_exists(thread_id: str, user_id: str) -> bool:
    db = get_db()
    try:
        return bool(get_thread_for_user(thread_id, user_id, db=db))
    finally:
        db.close()


async def _patch_model(thread_id: str, user_id: str, body: dict):
    other_mutations = sorted(_METADATA_MUTATIONS.intersection(body))
    if other_mutations:
        raise HTTPException(
            400,
            "model must be patched by itself; separate the model switch "
            f"from: {', '.join(other_mutations)}",
        )
    raw_model = body.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise HTTPException(
            422,
            "model is required: pass an explicit canonical model id",
        )
    model = raw_model.strip()
    checkpoint_choice = body.get("checkpoint_model")
    if "checkpoint_model" in body and (
        not isinstance(checkpoint_choice, str)
        or checkpoint_choice not in {"current", "target"}
    ):
        raise HTTPException(422, "checkpoint_model must be current or target")
    source = body.get("source")
    if "source" in body and source != "setup":
        raise HTTPException(422, "source must be setup when given")
    from connections.harness_refresh import prepare_selection

    await prepare_selection(model)
    if not is_dispatchable_model(model):
        raise HTTPException(422, dispatch_form_error(model))
    if not await run_in_db(lambda: _owned_thread_exists(thread_id, user_id)):
        raise HTTPException(404)
    try:
        # One door, and it never asks. A switch that needs a checkpoint folds
        # on the model being left; the explicit override remains available.
        result = await switch_model(
            thread_id,
            model,
            user_id=user_id,
            actor_kind="user",
            actor_id=user_id,
            reason="harness_model_switch",
            checkpoint_override=checkpoint_choice,
            setup_selection=source == "setup",
            **({"account_id": body["account_id"]} if body.get("account_id") is not None else {}),
        )
    except LocalityRefused as exc:
        return locality_refused_response(exc)
    except (ModelSwitchPreparationError, ModelSwitchConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not isinstance(result, ModelSwitchOutcome):
        from infra.model_switch_types import SwitchRequest

        return JSONResponse(
            status_code=202,
            content={
                "operation_id": result.operation_id,
                "status": result.status,
                "pending_model": getattr(result, "destination_model", model),
                "checkpoint_required": (
                    isinstance(result, SwitchRequest) and not result.waiting_for_source
                ),
            },
        )
    response: dict[str, object] = {"ok": True, "model": result.new_model}
    if result.committed_at:
        response["model_updated_at"] = result.committed_at
    if result.post_effects_error:
        response["warning"] = (
            "model switch committed, but post-switch cleanup or "
            f"notification failed: {result.post_effects_error}"
        )
    return response


def _write_project_patch(db, thread_id, project_id, old_project_id, now):
    """Commit project truth, binding holds and plan rehome together."""
    from connections.account_holds import refresh_thread_holds
    from plans.public import rehome

    if project_id and not db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone():
        return {"error": "bad_project_id", "detail": f"Project not found: {project_id}"}
    db.execute("UPDATE threads SET project_id=?,updated_at=? WHERE id=?", (project_id, now, thread_id))
    refresh_thread_holds(thread_id, db=db)
    context_changed = project_id != old_project_id
    if context_changed:
        rehome(thread_id, db=db)
    return context_changed


def _write_metadata_patch(
    thread_id: str,
    user_id: str,
    body: dict,
    now: str,
    drives: list[str] | None = None,
):
    with (
        write_transaction(None, scope="thread_metadata_patch") as db,
        public.collect_wake_recipients(db, drives),
    ):
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
        if not thread_row:
            return "not_found"
        old_project_id = dict(thread_row).get("project_id")
        title = body.get("title")
        project_id = body.get("project_id")
        changed = False
        title_changed = bool(title and title != thread_row["title"])
        context_boundary_changed = False
        archive_changed = False
        if title_changed:
            db.execute(
                "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, thread_id),
            )
            changed = True
        if "project_id" in body:
            project_result = _write_project_patch(db, thread_id, project_id, old_project_id, now)
            if isinstance(project_result, dict):
                return project_result
            changed = True
            context_boundary_changed = project_result
        if "archived" in body:
            from core.thread_lifecycle import ThreadLifecycleError, set_user_archive_state

            try:
                archive_changed = set_user_archive_state(
                    db, thread_id, archived=bool(body["archived"]), now=now
                )
            except ThreadLifecycleError as exc:
                return {"error": "lifecycle_conflict", "detail": str(exc)}
            changed = changed or archive_changed
        if "reasoning_effort" in body:
            db.execute(
                "UPDATE threads SET reasoning_effort = ?, updated_at = ? WHERE id = ?",
                (body["reasoning_effort"], now, thread_id),
            )
            changed = True
        if "pinned" in body:
            db.execute(
                "UPDATE threads SET pinned = ? WHERE id = ?",
                (1 if body["pinned"] else 0, thread_id),
            )
            changed = True
        roster_changed = title_changed or context_boundary_changed or archive_changed
        if roster_changed:
            updated = db.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,),
            ).fetchone()
            append_agent_roster_transition(
                db,
                before=thread_row,
                after=updated,
                summary=f"Agent responsibility map updated: {updated['title'] or 'Untitled'}",
            )
        return {
            "status": "ok",
            "context_boundary_changed": context_boundary_changed,
            "archive_changed": archive_changed,
        }


def _write_local_only(thread_id: str, user_id: str, local_only: bool) -> dict | str:
    """Write Local only on the thread and its family descendants in one transaction."""
    with write_transaction(None, scope="thread_local_only") as db:
        thread = get_thread_for_user(thread_id, user_id, db=db)
        if not thread:
            return "not_found"
        if local_only:
            for model in (thread["model"], thread["pending_model"]):
                require_local_only_model(model, thread_local_only=True, db=db, thread_id=thread_id)
        elif thread_id in local_only_sources([thread_id], db=db):
            return "ancestor_local_only"
        write_family_local_only(thread_id, local_only, utc_now_iso(), db=db)
        return {
            "local_only": local_only,
            "threads": family_local_only_payload(thread_id, db=db),
        }


@router.put("/threads/{thread_id}/local-only")
async def set_local_only(
    thread_id: str,
    body: dict,
    user: dict = Depends(require_human_user),
):
    """Set a thread's person-only Local only scope for it and every worker under it."""
    local_only = body.get("local_only")
    if not isinstance(local_only, bool):
        raise HTTPException(422, "local_only must be a boolean")
    try:
        result = await run_in_db(_write_local_only, thread_id, user["id"], local_only)
    except LocalityRefused as exc:
        return locality_refused_response(exc, turning_on=True)
    if result == "not_found":
        raise HTTPException(404)
    if result == "ancestor_local_only":
        return JSONResponse(
            status_code=409,
            content={
                "code": "local_only_set_by_parent",
                "message": "This chat is Local only because the chat that started it is. "
                "Turn Local only off there, or start a new chat.",
            },
        )
    emit_product_event(
        "session",
        "local_only.changed",
        thread_id=thread_id,
        user_id=user["id"],
        properties={"local_only": local_only},
    )
    return {"ok": True, **result}


async def _patch_metadata(thread_id: str, user_id: str, body: dict):
    title = body.get("title")
    woken: list[str] = []
    result = await run_in_db(
        _write_metadata_patch, thread_id, user_id, body, utc_now_iso(), woken
    )
    # Archiving a delegated child wakes its parent inside that write.
    await public.drive_threads(woken)
    if result == "not_found":
        raise HTTPException(404)
    if "error" in result:
        status_code = 400 if result["error"] == "bad_project_id" else 409
        raise HTTPException(status_code, result["detail"])
    if result.get("context_boundary_changed"):
        await runtime_boundary.post_context_boundary_effects(
            thread_id, user_id=user_id, reason="session_context_changed"
        )
    if title:
        emit_product_event(
            "session",
            "renamed",
            thread_id=thread_id,
            user_id=user_id,
            properties={"title_len": len(title)},
        )
    if body.get("archived") and result.get("archive_changed"):
        emit_product_event(
            "session",
            "ended",
            thread_id=thread_id,
            user_id=user_id,
            properties={"reason": "archived"},
        )
    return {"ok": True}


@router.patch("/threads/{thread_id}")
async def update_thread(thread_id: str, token: str, body: dict):
    """Update mutable thread fields through the single model-switch door."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if "model" in body:
        return await _patch_model(thread_id, user["id"], body)
    return await _patch_metadata(thread_id, user["id"], body)


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, token: str):
    """Delete an owned thread and its thread-scoped runtime state."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    woken: list[str] = []
    try:
        status = await _delete_owned_thread(thread_id, user["id"], woken)
    except ThreadQuiescenceError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        # A delete wakes the parent it leaves; a refused one, the family it fenced.
        await public.drive_threads(woken)
    if status == "thread_not_found":
        raise HTTPException(404)
    if status == "turn_raced":
        raise HTTPException(409, "A successor turn raced session deletion")
    emit_product_event(
        "session", "deleted", thread_id=thread_id, user_id=user["id"]
    )
    return {"ok": True}
