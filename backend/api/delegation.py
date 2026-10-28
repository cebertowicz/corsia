"""REST routes for visible delegated threads."""

from __future__ import annotations

from infra.time import utc_now_iso

from fastapi import APIRouter, HTTPException
from loguru import logger

from api.locality_refusal import agent_locality_refused_response
from infra.auth import authenticate, owns_thread
from infra.database import get_db
from infra.db_async import run_in_db
from infra.conductor import get_coordination_scopes
from infra.delegation import DelegationModelRequired, require_explicit_thread_model
from infra.delegation_scope import target_scope_authority, validate_send_target_scope
from infra.execution_class import is_subagent
from infra.fresh_threads import (
    SpawnVisibilityRejected,
    create_visible_fresh_thread,
)
from infra.local_only import LocalityRefused, send_confirmation_error
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from infra.runtime_truth import read_runtime_truth
from infra.send_service import SendRejected, send_thread_input
from infra.send_types import InboxQueuedInput
from llm.catalog import dispatch_form_error, is_dispatchable_model

router = APIRouter(prefix="/api/delegation", tags=["delegation"])


def _delegated_sender(sender_id: str, user_id: str) -> dict | None:
    db = get_db()
    try:
        row = db.execute(
            "SELECT title, project_id, local_only FROM threads WHERE id = ? AND user_id = ?",
            (sender_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": sender_id,
            "title": row["title"] or "",
            "project_id": row["project_id"] or None,
            "local_only": bool(row["local_only"]),
            "scope": row["project_id"] or "workspace",
        }
    finally:
        db.close()


def _delegated_target(thread_id: str, user_id: str) -> dict | None:
    db = get_db()
    try:
        target = get_thread_for_user(thread_id, user_id, db=db)
        if not target:
            return None
        project_id = target["project_id"] or None
        return {
            "thread_row": dict(target),
            "target_authority": target_scope_authority(project_id, db=db),
        }
    finally:
        db.close()


def _queued_send_response(thread_id: str, result: InboxQueuedInput) -> dict:
    receipt = result.receipt
    payload = {
        "ok": True,
        "status": "queued",
        "queue_kind": "messages",
        "thread_id": thread_id,
        "duplicate": result.duplicate,
        "queued": {
            "message_id": receipt.message_id,
            "queue_order": receipt.input_order,
            "created_at": receipt.created_at,
            "delivery_revision": receipt.delivery_revision,
        },
    }
    if result.held_line is not None:
        payload["held"] = result.held_line
    return payload


async def _calling_agent(from_thread_id: str | None, user_id: str) -> str | None:
    """The caller's own thread, owned by the authenticated user; it governs what it may open."""
    if from_thread_id and not await run_in_db(owns_thread, from_thread_id, user_id):
        raise HTTPException(
            403, "from_thread_id: thread not found or not owned by the authenticated user",
        )
    return from_thread_id


async def _require_send_confirmation(sender: dict, recipient: dict, body: dict) -> None:
    error = await run_in_db(
        send_confirmation_error, sender, recipient, body.get("no_sensitive_content"),
    )
    if error:
        raise HTTPException(409, error)


@router.post("/threads")
async def create_thread(token: str, body: dict):
    """Create a visible chat or hidden child with an explicit model.

    Subagents require ``spawned_by`` or ``from_thread_id``; a chat's
    ``spawned_by`` is kept as its ``parent_thread_id``. ``provider`` is
    informational because the exact model id identifies its provider.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    thread_kind = (body.get("thread_kind") or "chat").strip()
    if thread_kind not in {"chat", "subagent"}:
        raise HTTPException(422, "thread_kind must be chat or subagent")
    try:
        model = require_explicit_thread_model(body.get("model"))
    except DelegationModelRequired as e:
        raise HTTPException(422, str(e))

    # Fail loud before persisting threads.model: a non-dispatchable form (e.g.
    # a catalog selection id ``omlx/<id>`` instead of its dispatch form
    # ``omlx@<server>/<model>``) would create a thread that silently no-ops at
    # native dispatch. Reject at create so the doomed thread never exists.
    if not is_dispatchable_model(model):
        raise HTTPException(422, dispatch_form_error(model))

    now = utc_now_iso()
    project_id = body.get("project_id") or None
    title = body.get("title") or "New Chat"
    spawned_by = (body.get("spawned_by") or "").strip() or None
    from_thread_id = await _calling_agent((body.get("from_thread_id") or "").strip() or None, user["id"])
    parent_thread_id: str | None = None

    # A subagent that has no parent is an invisible orphan: hidden from the
    # sidebar (spawned_by IS NOT NULL → subagent) but unreachable from any spawn
    # chip. Parent presence (spawned_by) is the single source of truth for
    # subagent-ness, so thread_kind=subagent is only honored when a parent
    # resolves.
    # Auto-promote from_thread_id → spawned_by when the caller omits spawned_by
    # but supplies its own thread id as from_thread_id. If neither is present,
    # reject loud so the bug surfaces at creation.
    if thread_kind == "subagent":
        if not spawned_by and from_thread_id:
            spawned_by = from_thread_id
        elif spawned_by and from_thread_id and spawned_by != from_thread_id:
            logger.warning(
                "delegation create_thread: spawned_by={} and from_thread_id={} "
                "both present but differ — spawned_by wins; from_thread_id ignored",
                spawned_by, from_thread_id,
            )
        if not spawned_by:
            raise HTTPException(
                422,
                "thread_kind=subagent requires spawned_by or from_thread_id: "
                "a parentless subagent is invisible. "
                "Pass spawned_by=<parent-thread-id> or use thread_kind=chat "
                "for top-level threads the user will interact with directly.",
            )
    elif spawned_by:
        # ENFORCE the single-source-of-truth invariant the sidebar relies on:
        # spawned_by marks a HIDDEN subagent, so a non-subagent (coordinator /
        # chat / peer) thread must never carry it — otherwise it would silently
        # vanish from the sidebar. But such a thread may legitimately HAVE a
        # parent (e.g. a Coordinator spawned by a Conductor): preserve that
        # lineage in parent_thread_id (ownership-only — does NOT affect
        # visibility) instead of dropping it, so the thread stays visible AND its
        # provenance is recorded. (To create a hidden child, use
        # thread_kind=subagent.)
        # Re-home BYPASSES create_visible_fresh_thread's spawner-ownership guard
        # (which only checks spawned_by), so validate parent ownership here — a
        # visible thread must not point its lineage at a foreign-user thread.
        def _parent_owned() -> bool:
            db = get_db()
            try:
                return db.execute(
                    "SELECT id FROM threads WHERE id = ? AND user_id = ?",
                    (spawned_by, user["id"]),
                ).fetchone() is not None
            finally:
                db.close()

        if not await run_in_db(_parent_owned):
            raise HTTPException(
                403,
                "spawned_by: thread not found or not owned by the authenticated user",
            )
        parent_thread_id = spawned_by
        spawned_by = None

    parent_for_scope = spawned_by or parent_thread_id
    if project_id is None and parent_for_scope:
        def _parent_project_id():
            db = get_db()
            try:
                row = db.execute(
                    "SELECT project_id FROM threads WHERE id = ? AND user_id = ?",
                    (parent_for_scope, user["id"]),
                ).fetchone()
                if row is None:
                    return "__not_found__"
                return row["project_id"] or None
            finally:
                db.close()

        inherited_project_id = await run_in_db(_parent_project_id)
        if inherited_project_id == "__not_found__":
            raise HTTPException(
                403,
                "spawned_by: thread not found or not owned by the authenticated user",
            )
        project_id = inherited_project_id

    try:
        thread_row = await create_visible_fresh_thread(
            user_id=user["id"],
            profile=body.get("profile", "personal"),
            title=title,
            model=model,
            project_id=project_id,
            spawned_by=spawned_by,
            parent_thread_id=parent_thread_id,
            created_at=now, actor_thread_id=from_thread_id,
            thread_kind=thread_kind,
            emit_spawn_chip=bool(spawned_by), account_id=body.get("account_id"),
        )
    except LocalityRefused as exc:
        return agent_locality_refused_response(exc)
    except SpawnVisibilityRejected as exc:
        raise HTTPException(403, str(exc)) from exc
    thread_id = thread_row["id"]

    emit_product_event(
        "session", "started",
        thread_id=thread_id, user_id=user["id"],
        properties={"source": "delegation", "project_id": project_id,
                    "thread_kind": thread_kind, "model": model,
                    "spawned_by": spawned_by},
    )
    return {"thread_id": thread_id, "model": model, "thread_kind": thread_kind,
            "title": title, "project_id": project_id, "spawned_by": spawned_by}


@router.post("/threads/{thread_id}/send")
async def send_turn(thread_id: str, token: str, body: dict):
    """Admit one attributed agent message to a task's durable FIFO."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "Missing 'message' in request body")

    from_thread_id = (body.get("from_thread_id") or "").strip() or None
    if not from_thread_id:
        raise HTTPException(
            400,
            "from_thread_id is required: a delegated thread-to-thread /send must "
            "carry verified sender attribution. Pass from_thread_id=<your thread "
            "id> (the corsia delegate CLI auto-fills it from CORSIA_THREAD_ID, "
            "or use --from-thread-id). Human messages go through the chat "
            "WebSocket, not this endpoint.",
        )

    sender = await run_in_db(_delegated_sender, from_thread_id, user["id"])
    if sender is None:
        raise HTTPException(
            403,
            "from_thread_id: thread not found or not owned by the authenticated user",
        )
    agent_meta = {
        "sender_kind": "agent",
        "from_thread": from_thread_id,
        "from_title": sender["title"],
        "scope": sender["scope"],
    }

    target_payload = await run_in_db(_delegated_target, thread_id, user["id"])
    if not target_payload:
        raise HTTPException(404, "Thread not found")
    thread_row = target_payload["thread_row"]

    target_project_id = thread_row["project_id"] or None
    scope_check = validate_send_target_scope(
        source_project_id=sender["project_id"],
        target_project_id=target_project_id,
        target_thread_id=thread_id,
        requested_target_scope_id=body.get("target_scope_id"),
        target_authority=target_payload["target_authority"],
    )
    if not scope_check["ok"]:
        raise HTTPException(409, scope_check["detail"])

    await _require_send_confirmation(sender, thread_row, body)

    target_kind = "subagent" if is_subagent(thread_row) else "chat"
    try:
        result = await send_thread_input(
            thread_id,
            thread_row,
            message,
            user=user,
            metadata=agent_meta,
            idempotency_key=body.get("idempotency_key"),
            native_mode="delegation",
            accepted_effects={
                "send_ref": {
                    "sender_thread_id": agent_meta["from_thread"],
                    "target_title": thread_row["title"] or "Untitled thread",
                    "target_kind": target_kind,
                }
            },
        )
    except SendRejected as exc:
        raise HTTPException(exc.status_code, exc.detail)
    if isinstance(result, InboxQueuedInput):
        return _queued_send_response(thread_id, result)
    return {
        "ok": True,
        "thread_id": thread_id,
        "driver": result.driver,
        "duplicate": result.duplicate,
    }


@router.get("/threads/{thread_id}/status")
async def thread_status(thread_id: str, token: str, messages: int = 5):
    """Read a thread's turn-state + most-recent messages (delegation-shaped).

    Full history lives on ``GET /api/threads/{id}/messages`` — this is the
    thin polling view after a ``send``: is a turn active, is the provider
    runtime live, and the last few messages.

    Query: ``messages`` — how many recent messages to include (default 5).
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    n = max(0, min(messages, 50))

    from api.thread_status_read import read_status

    result = await run_in_db(read_status, thread_id, user, n)
    if result is None:
        raise HTTPException(404, "Thread not found")
    thread_row, active, conductor_scopes, recent = result

    truth = await read_runtime_truth(
        thread_id, thread_row,
        active_turn=active,
        conductor_scopes=conductor_scopes,
    )
    # Hidden workers: spawned_by is the canonical parent and the subagent marker;
    # parent_thread_id is visible-thread lineage only.
    parent_id = thread_row.get("spawned_by") or thread_row.get("parent_thread_id")
    return {
        "thread_id": thread_id,
        "title": thread_row.get("title"),
        "parent_thread_id": parent_id or None,
        **truth,
        "turn_active": active is not None,
        "active_turn": active,
        "recent_messages": recent,
    }
