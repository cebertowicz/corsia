"""Authenticated delegated model switches."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from infra.auth import authenticate
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from thread.model import switch_model
from thread.model_row import continuity_note
from thread.model_state import (
    ModelSwitchConflict,
    ModelSwitchOutcome,
    ModelSwitchPreparationError,
)
from llm.catalog import dispatch_form_error, is_dispatchable_model

router = APIRouter(prefix="/api/delegation", tags=["delegation"])


def _request_values(body: dict) -> tuple[str, str | None, str, str]:
    raw_model = body.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    if not model:
        raise HTTPException(
            422,
            "model is required: pass the explicit target dispatch form (e.g. "
            "claude-code/opus, anthropic/<id>, codex/gpt-5-codex). There is no "
            "silent default.",
        )
    if not is_dispatchable_model(model):
        raise HTTPException(422, dispatch_form_error(model))
    checkpoint_override = body.get("checkpoint_model")
    if checkpoint_override is not None and checkpoint_override != "target":
        raise HTTPException(
            422, "checkpoint_model may be omitted (current) or set to target",
        )
    raw_sender = body.get("from_thread_id")
    sender = (raw_sender.strip() if isinstance(raw_sender, str) else None) or None
    if not sender:
        raise HTTPException(
            400,
            "from_thread_id is required: a delegated set-model must carry "
            "verified sender attribution. Pass from_thread_id=<your thread id> "
            "(the corsia delegate CLI auto-fills it from CORSIA_THREAD_ID, or "
            "use --from-thread-id).",
        )
    raw_reason = body.get("reason")
    reason = (raw_reason.strip() if isinstance(raw_reason, str) else "") or "resume-recovery"
    return model, checkpoint_override, sender, reason


def _authorize_switch(thread_id: str, sender_id: str, user_id: str):
    db = get_db()
    try:
        sender = db.execute(
            "SELECT title FROM threads WHERE id = ? AND user_id = ?",
            (sender_id, user_id),
        ).fetchone()
        if sender is None:
            return "bad_sender"
        if not get_thread_for_user(thread_id, user_id, db=db):
            return "not_found"
        return {"actor_title": sender["title"] or ""}
    finally:
        db.close()


def _pending_response(thread_id: str, model: str, checkpoint_override, result):
    from infra.model_switch_types import SwitchRequest

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "thread_id": thread_id,
            "target_model": model,
            "switched": False,
            "checkpoint_model": "target" if checkpoint_override == "target" else "current",
            "operation_id": result.operation_id,
            "status": result.status,
            "pending_model": getattr(result, "destination_model", model),
            "checkpoint_required": not isinstance(result, SwitchRequest),
            "instruction": (
                "Call delegate_status with operation_id="
                f"{result.operation_id} to observe completion or failure."
            ),
        },
    )


def _committed_response(thread_id: str, outcome: ModelSwitchOutcome) -> dict:
    projection_mode = (
        outcome.preparation.projection_mode if outcome.preparation is not None else None
    )
    response: dict[str, object] = {
        "ok": True,
        "thread_id": thread_id,
        "old_model": outcome.old_model,
        "new_model": outcome.new_model,
        "changed": outcome.changed,
        "model_updated_at": outcome.committed_at,
        "continuity": continuity_note(projection_mode=projection_mode),
    }
    if outcome.post_effects_error:
        response["warning"] = (
            "model switch committed, but post-switch cleanup or "
            f"notification failed: {outcome.post_effects_error}"
        )
    return response


@router.post("/threads/{thread_id}/set-model")
async def set_thread_model(thread_id: str, token: str, body: dict):
    """Switch one owned task to an explicit dispatchable model."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    new_model, checkpoint_override, from_thread_id, reason = _request_values(body)
    authorization = await run_in_db(
        _authorize_switch, thread_id, from_thread_id, user["id"],
    )
    if authorization == "bad_sender":
        raise HTTPException(
            403,
            "from_thread_id: thread not found or not owned by the authenticated user",
        )
    if authorization == "not_found":
        raise HTTPException(404, "Thread not found")
    try:
        result = await switch_model(
            thread_id,
            new_model,
            user_id=user["id"],
            actor_kind="agent",
            actor_id=from_thread_id,
            reason=reason,
            checkpoint_override=checkpoint_override,
            **({"account_id": body["account_id"]} if body.get("account_id") is not None else {}),
        )
    except (ModelSwitchPreparationError, ModelSwitchConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not isinstance(result, ModelSwitchOutcome):
        return _pending_response(thread_id, new_model, checkpoint_override, result)
    return _committed_response(thread_id, result)
