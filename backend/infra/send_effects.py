"""Bounded side effects that run only after durable send acceptance."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from infra import db_async
from infra.send_types import SendRejected


_ACCEPTED_EFFECT_TASKS: set[asyncio.Task] = set()
_ACCEPTED_TITLE_TASKS: dict[str, asyncio.Task] = {}


def _validated_send_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SendRejected(400, "send_ref accepted effect must be an object")
    unknown = set(value) - {"sender_thread_id", "target_title", "target_kind"}
    if unknown:
        raise SendRejected(
            400,
            f"unknown send_ref fields: {sorted(unknown)}; valid fields are "
            "sender_thread_id, target_kind, target_title",
        )
    sender_thread_id = value.get("sender_thread_id")
    target_title = value.get("target_title")
    target_kind = value.get("target_kind")
    if not isinstance(sender_thread_id, str) or not sender_thread_id:
        raise SendRejected(400, "send_ref sender_thread_id is required")
    if not isinstance(target_title, str) or not target_title:
        raise SendRejected(400, "send_ref target_title is required")
    if target_kind is not None and not isinstance(target_kind, str):
        raise SendRejected(400, "send_ref target_kind must be a string")
    return {
        "sender_thread_id": sender_thread_id,
        "target_title": target_title,
        "target_kind": target_kind,
    }


def validate_accepted_effects(accepted_effects: dict | None) -> dict | None:
    """Validate and detach the bounded post-acceptance effect descriptor."""
    if accepted_effects is None:
        return None
    if not isinstance(accepted_effects, dict):
        raise SendRejected(400, "accepted send effects must be an object")
    unknown = set(accepted_effects) - {"title", "uploads", "send_ref"}
    if unknown:
        raise SendRejected(400, f"unknown accepted send effects: {sorted(unknown)}")
    normalized: dict[str, Any] = {}
    for name in ("title", "uploads"):
        if name in accepted_effects:
            enabled = accepted_effects[name]
            if not isinstance(enabled, bool):
                raise SendRejected(400, f"{name} accepted effect must be boolean")
            normalized[name] = enabled
    if "send_ref" in accepted_effects:
        normalized["send_ref"] = _validated_send_ref(accepted_effects["send_ref"])
    try:
        json.dumps(normalized, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SendRejected(
            400,
            "accepted send effects must be JSON serializable",
        ) from exc
    return normalized


def _log_title_effect_result(task: asyncio.Task, thread_id: str) -> None:
    _ACCEPTED_EFFECT_TASKS.discard(task)
    if _ACCEPTED_TITLE_TASKS.get(thread_id) is task:
        _ACCEPTED_TITLE_TASKS.pop(thread_id, None)
    if task.cancelled():
        logger.warning("accepted send title effect was cancelled for {}", thread_id)
        return
    error = task.exception()
    if error is not None:
        logger.error("accepted send title effect failed for {}: {}", thread_id, error)


async def _register_upload_effect(
    thread_id: str,
    attachments: list | None,
    source_input_id: str,
) -> None:
    from registry.service import register_uploads

    try:
        await db_async.run_in_db(
            register_uploads,
            thread_id,
            attachments or [],
            source_input_id=source_input_id,
        )
    except Exception:
        logger.exception(
            "accepted send upload registration failed for {} ({})",
            thread_id,
            source_input_id,
        )


def _schedule_title_effect(
    thread_id: str,
    thread_row: Any,
    source_input_id: str,
) -> None:
    from ws.post_turn import start_title_early

    try:
        title_task = _ACCEPTED_TITLE_TASKS.get(thread_id)
        if title_task is None or title_task.done():
            title_task = asyncio.create_task(
                start_title_early(thread_id, str(thread_row["user_id"]))
            )
            _ACCEPTED_TITLE_TASKS[thread_id] = title_task
            _ACCEPTED_EFFECT_TASKS.add(title_task)
            title_task.add_done_callback(
                lambda task: _log_title_effect_result(task, thread_id)
            )
    except Exception:
        logger.exception(
            "accepted send title effect scheduling failed for {} ({})",
            thread_id,
            source_input_id,
        )


async def _emit_send_ref_effect(
    thread_id: str,
    send_ref: dict,
    source_input_id: str,
) -> None:
    from infra.delegation_markers import emit_send_ref_marker

    try:
        await emit_send_ref_marker(
            send_ref["sender_thread_id"],
            thread_id,
            send_ref["target_title"],
            send_ref["target_kind"],
            source_input_id=source_input_id,
        )
    except Exception:
        logger.exception(
            "accepted send reference effect failed for {} ({})",
            thread_id,
            source_input_id,
        )


async def run_send_accepted_effects(
    *,
    thread_id: str,
    thread_row: Any,
    attachments: list | None,
    accepted_effects: dict | None,
    source_input_id: str,
) -> None:
    """Run bounded effects after acceptance without falsifying send failure."""
    if not accepted_effects:
        return
    if accepted_effects.get("uploads"):
        await _register_upload_effect(thread_id, attachments, source_input_id)
    if accepted_effects.get("title"):
        _schedule_title_effect(thread_id, thread_row, source_input_id)
    send_ref = accepted_effects.get("send_ref")
    if send_ref is not None:
        await _emit_send_ref_effect(thread_id, send_ref, source_input_id)


async def run_batch_accepted_effects(batch, thread_row: Any) -> None:
    """Run each accepted FIFO member's effects with only public attachments."""
    from core.media_delivery import public_attachments

    for item in batch.items:
        options = item.delivery_options
        await run_send_accepted_effects(
            thread_id=batch.thread_id,
            thread_row=thread_row,
            attachments=public_attachments(options.get("attachments") or []),
            accepted_effects=options.get("accepted_effects"),
            source_input_id=item.message_id,
        )
