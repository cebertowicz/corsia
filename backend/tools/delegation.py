"""Delegation-native tools for creating, sending to, and repairing threads."""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db
from infra.delegation_status import held_admission_status, model_switch_status
from infra.fresh_threads import create_visible_fresh_thread
from infra.queries import get_thread_for_user
from infra.send_service import SendRejected, send_thread_input
from infra.send_types import InboxQueuedInput
from infra.thread_input_types import held_admission_line
from llm.delegation_model import dispatch_scope, eligible_delegation_model
from tools.base import Tool
from tools.delegation_completion import resolve_child_turn_for_parent


def _clean(value: Any) -> str | None:
    text = (value or "").strip() if isinstance(value, str) else ""
    return text or None


def _requested_model_id(model: Any, provider: Any = None) -> str | None:
    """Normalize model/provider input into the picker model id."""
    model_id = _clean(model)
    provider_id = _clean(provider)
    if not model_id:
        return None
    if provider_id and "/" not in model_id and "@" not in model_id:
        return f"{provider_id}/{model_id}"
    return model_id


class _NotClaudeCodeModel(ValueError):
    """A resolved catalog target belongs to another execution harness."""


def _is_cc_model(model_id: str | None) -> bool:
    if not model_id:
        return False
    try:
        _resolve_cc_target(model_id)
    except _NotClaudeCodeModel:
        return False
    return True


def _resolve_cc_target(model_id: str):
    from llm.resolution import (
        ModelUnavailableError,
        resolve_execution_target,
        resolve_harness_selector,
    )

    try:
        target = resolve_execution_target(model_id)
    except ModelUnavailableError as exc:
        if not model_id.startswith("claude-code/"):
            raise _NotClaudeCodeModel(
                f"{model_id!r} is not a Claude Code model"
            ) from exc
        target = resolve_harness_selector("claude-code", model_id)
    if target.harness != "claude-code":
        raise _NotClaudeCodeModel(f"{model_id!r} is not a Claude Code model")
    return target


def _is_delisted(model_id: str | None) -> bool:
    from infra.delegation import is_delisted_model

    return is_delisted_model(model_id)


def _current_thread_context(thread_id: str) -> dict[str, Any] | None:
    """Return current thread facts used as the delegation actor."""
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT id, user_id, profile, project_id, title, agent_enabled, agent_status, "
            "lifecycle_state, archived_reason, spawned_by FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def _require_context(kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | str:
    thread_id = kwargs.get("_thread_id")
    if not thread_id:
        return "Error: delegation tools must be called from within a conversation."
    ctx = _current_thread_context(thread_id)
    if not ctx:
        return f"Error: current conversation '{thread_id}' was not found."
    return thread_id, ctx


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def _recent_message(row: Any) -> dict:
    """One recent row, with a tool-calling Step's words instead of a blank."""
    from core.message_display import durable_row_text

    message = dict(row)
    message["content"] = durable_row_text(message["content"], message.pop("metadata"))
    return message


def _queued_result(thread_id: str, result: InboxQueuedInput) -> dict:
    """Render one durable message-FIFO receipt and any admission hold."""
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


def _tool_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


# Keep the established private name at this module's call sites.
_resolve_child_turn_for_parent = resolve_child_turn_for_parent


async def _send_explicit_completion(
    child_thread_id: str,
    parent_thread_id: str,
    child_turn_id: str | None,
    content: str,
) -> str:
    """Admit one completion message; that message is the idempotency record."""
    if not child_turn_id:
        return (
            "Error: completion intent is valid only from a delegated child to "
            "its registered parent during an active turn"
        )
    from plans.public import has_open_structured_attachment

    if await run_in_db(has_open_structured_attachment, child_thread_id):
        return (
            "Error: your plan's return already delivers your result to the "
            "parent; nothing was sent. Put the result in your step's review "
            "evidence (register an artifact and name it there if it is long), "
            "then stop"
        )
    from infra.database import get_db

    def _parent() -> dict | None:
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM threads WHERE id=?",
                (parent_thread_id,),
            ).fetchone()
            return None if row is None else dict(row)
        finally:
            db.close()

    parent = await run_in_db(_parent)
    if parent is None:
        return "Error: registered parent thread no longer exists"
    metadata = {
        "sender_kind": "agent",
        "from_thread": child_thread_id,
        "completion": True,
        "child_turn_id": child_turn_id,
        "terminal_status": "completed",
    }
    try:
        result = await send_thread_input(
            parent_thread_id,
            parent,
            content,
            user={"id": str(parent.get("user_id") or "")},
            metadata=metadata,
            idempotency_key=f"parent-completion:{child_turn_id}",
            native_mode="delegation",
        )
    except SendRejected as exc:
        return f"Error: {exc.detail}"
    if isinstance(result, InboxQueuedInput):
        queued = _queued_result(parent_thread_id, result)
        return _json(queued | {"completion": "accepted"})
    return _json({
        "ok": True, "thread_id": parent_thread_id,
        "completion": "accepted", "duplicate": False,
    })


class DelegateCreateThreadTool(Tool):
    """Create a delegated CORSIA thread."""

    @property
    def name(self) -> str:
        return "delegate_create_thread"

    @property
    def description(self) -> str:
        return (
            "Create a CORSIA thread for delegated work. Use thread_kind=chat for "
            "a visible thread the user may enter; use thread_kind=subagent for a "
            "hidden child and provide parent_thread_id or rely on the current thread."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema(
            {
                "title": {"type": "string", "description": "Thread title"},
                "project_id": {
                    "type": ["string", "null"], "description": "Project scope. Defaults to the current thread's project.",
                },
                "model": {
                    "type": "string", "pattern": r"\S",
                    "description": "Required explicit dispatchable model id; there is no system default.",
                },
                "account_id": {"type": ["string", "null"], "description": "Optional permitted vendor account; separate from the exact model."},
                "provider": {
                    "type": ["string", "null"],
                    "description": "Optional provider prefix for shorthand model names.",
                },
                "thread_kind": {
                    "type": "string",
                    "enum": ["chat", "subagent"],
                    "description": "chat is visible; subagent is hidden and parented.",
                },
                "parent_thread_id": {
                    "type": ["string", "null"],
                    "description": "Parent/provenance thread. Defaults to current thread for subagents.",
                },
            },
            ["model"],
        )

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        current_id, current = context
        thread_kind = kwargs.get("thread_kind") or "chat"
        if thread_kind not in {"chat", "subagent"}:
            return "Error: thread_kind must be chat or subagent"
        from infra.delegation import DelegationModelRequired, require_explicit_thread_model

        requested = _requested_model_id(kwargs.get("model"), kwargs.get("provider"))
        try:
            model = require_explicit_thread_model(requested)
        except DelegationModelRequired as e:
            return f"Error: {e}"
        model, refusal = await run_in_db(lambda: eligible_delegation_model(model, **dispatch_scope(kwargs, current.get("project_id"))))
        if refusal:
            return f"Error: {refusal}"
        parent_thread_id = _clean(kwargs.get("parent_thread_id"))
        spawned_by = parent_thread_id or current_id if thread_kind == "subagent" else None
        parent_thread_id = parent_thread_id if thread_kind == "chat" else None
        project_id = kwargs.get("project_id")
        if project_id is None:
            project_id = current.get("project_id")

        try:
            thread_row = await create_visible_fresh_thread(
                user_id=current["user_id"] or "",
                profile=current.get("profile") or "personal",
                title=kwargs.get("title") or "New Chat",
                model=model,
                project_id=project_id,
                spawned_by=spawned_by,
                parent_thread_id=parent_thread_id,
                thread_kind=thread_kind,
                emit_spawn_chip=bool(spawned_by),
                account_id=kwargs.get("account_id"), actor_thread_id=current_id,
            )
        except Exception as e:
            return f"Error creating delegated thread: {e}"
        return _json({
            "ok": True, "thread_id": thread_row["id"], "model": model,
            "thread_kind": thread_kind, "project_id": project_id,
            "spawned_by": spawned_by,
        })


def _checkpoint_status(checkpoint) -> dict[str, Any]:
    return {
        "id": checkpoint.id, "trigger": checkpoint.trigger, "mode": checkpoint.mode,
        "status": checkpoint.status, "writer_model": checkpoint.writer_model,
        "error": checkpoint.error, "started_at": checkpoint.started_at,
    }


def _read_delegate_status(thread_id: str, user_id: str, message_count: int):
    from infra.conductor import get_conductor_scopes
    from infra.checkpoint_queries import latest_for_thread
    from infra.database import get_db

    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
        if not thread_row:
            return None
        active = db.execute(
            "SELECT id, status, started_at FROM active_turns "
            "WHERE thread_id = ? AND status='running' "
            "ORDER BY started_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        recent = db.execute(
            "SELECT id, role, content, metadata, created_at FROM messages "
            "WHERE thread_id = ? AND delivery_state = 'delivered' "
            "ORDER BY created_at DESC LIMIT ?",
            (thread_id, message_count),
        ).fetchall() if message_count else []
        checkpoint = latest_for_thread(thread_id, db=db)
        thread = dict(thread_row)
        thread["held_admission"] = held_admission_status(db, thread, dict(active) if active else None)
        return (
            thread,
            dict(active) if active else None,
            get_conductor_scopes(thread_id, db=db),
            [_recent_message(row) for row in reversed(recent)],
            None if checkpoint is None else _checkpoint_status(checkpoint),
        )
    finally:
        db.close()


class DelegateStatusTool(Tool):
    """Read runtime truth and recent output for a delegated thread."""

    @property
    def name(self) -> str:
        return "delegate_status"

    @property
    def description(self) -> str:
        return (
            "Read a delegated thread's runtime status, its most recent "
            "checkpoint operation, and recent messages. It reports a held "
            "account-limit admission when a queued prefix cannot start. Pass "
            "checkpoint_id to prove whether that exact operation is still "
            "current."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema(
            {
                "thread_id": {"type": "string", "description": "Thread id"},
                "messages": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 50,
                    "description": "Recent messages to include",
                },
                "checkpoint_id": {
                    "type": ["string", "null"],
                    "description": "Optional exact checkpoint id to match",
                },
                "operation_id": {
                    "type": ["string", "null"],
                    "description": "Optional exact pending model-switch id to match",
                },
            },
            ["thread_id"],
        )

    async def execute(
        self,
        thread_id: str,
        messages: int = 5,
        checkpoint_id: str | None = None,
        operation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        _current_id, current = context
        from infra.runtime_truth import read_runtime_truth

        n = max(0, min(int(messages or 0), 50))
        result = await run_in_db(
            _read_delegate_status, thread_id, current["user_id"], n
        )
        if result is None:
            return f"Error: Thread not found: {thread_id}"
        thread_row, active, conductor_scopes, recent, checkpoint = result
        held = thread_row.pop("held_admission")
        model_switch = model_switch_status(thread_row)
        requested_checkpoint_id = _clean(checkpoint_id)
        requested_operation_id = _clean(operation_id)
        truth = await read_runtime_truth(
            thread_id,
            thread_row,
            active_turn=active,
            conductor_scopes=conductor_scopes,
        )
        return _json(
            {
                "thread_id": thread_id,
                "title": thread_row.get("title"),
                **truth,
                "turn_active": active is not None,
                "active_turn": active,
                "model_switch": model_switch,
                "requested_operation_id": requested_operation_id,
                "requested_operation_matches": (
                    None
                    if requested_operation_id is None
                    else model_switch is not None
                    and model_switch["operation_id"] == requested_operation_id
                ),
                "checkpoint": checkpoint,
                "requested_checkpoint_id": requested_checkpoint_id,
                "requested_checkpoint_matches": (
                    None
                    if requested_checkpoint_id is None
                    else checkpoint is not None
                    and checkpoint["id"] == requested_checkpoint_id
                ),
                "recent_messages": recent,
                **({"held": held_admission_line(held)} if held is not None else {}),
            }
        )


def _delegation_target_exists(thread_id: str, user_id: str) -> bool:
    from infra.database import get_db

    db = get_db()
    try:
        return bool(get_thread_for_user(thread_id, user_id, db=db))
    finally:
        db.close()


def _delegate_pending_switch(
    thread_id: str,
    model: str,
    checkpoint_override,
    result,
) -> str:
    from infra.model_switch_types import SwitchRequest

    return _json(
        {
            "accepted": True,
            "thread_id": thread_id,
            "target_model": model,
            "switched": False,
            "checkpoint_model": "target"
            if checkpoint_override == "target"
            else "current",
            "operation_id": result.operation_id,
            "status": result.status,
            "pending_model": getattr(result, "destination_model", model),
            "checkpoint_required": not isinstance(result, SwitchRequest),
            "instruction": (
                "Call delegate_status with operation_id="
                f"{result.operation_id} to observe completion or failure."
            ),
        }
    )


def _delegate_committed_switch(thread_id: str, outcome) -> str:
    from thread.model_row import continuity_note

    projection_mode = (
        outcome.preparation.projection_mode if outcome.preparation is not None else None
    )
    response: dict[str, Any] = {
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
    return _json(response)
