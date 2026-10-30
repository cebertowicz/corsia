"""Persist and project one native tool result at its Assistant Step boundary."""

from __future__ import annotations

from typing import Any

from infra.database import write_transaction
from infra.db_async import run_in_db
from tools.tool_display import SettledResult, project_tool_display, tool_done_frame


def _media_metadata(result: str) -> dict[str, Any] | None:
    """Return reference-only metadata for a typed media result."""
    reference = getattr(result, "media", None)
    return (
        {"media": reference.as_metadata()}
        if reference is not None
        else None
    )


def _persist_tool_row(
    handler: Any,
    *,
    write_transaction_fn: Any,
    payload: dict,
    full_result: str,
    media_metadata: dict[str, Any] | None,
) -> tuple[Any | None, Any]:
    """Write the native canonical row and run its non-gating observer."""
    from core._tool_result_delivery_support import metadata_object
    from core.tool_result_delivery import (
        persist_tool_execution_result,
        synthetic_result_identity,
    )
    from infra.tool_result_errors import is_error_result

    invocation_id, extra_metadata = payload["id"], {}
    if payload.get("synthetic_tool_result") is True:
        invocation_id, extra_metadata = synthetic_result_identity(
            tool_use_id=payload["id"], reason=payload.get("synthetic_reason"),
        )
    with write_transaction_fn(None, scope="persist_native_tool_result") as db:
        persisted = persist_tool_execution_result(
            thread_id=handler.thread_id,
            turn_id=handler.turn_id,
            invocation_id=invocation_id,
            assistant_step_id=handler.tool_step_id,
            provider_tool_call_id=payload["id"],
            name=payload["name"],
            arguments=None,
            content=full_result,
            is_error=payload.get("is_error") is True or is_error_result(full_result),
            extra_metadata={**extra_metadata, **(media_metadata or {})},
            db=db,
        )
        row = db.execute(
            "SELECT id,seq,created_at,metadata FROM messages WHERE id=? AND thread_id=?",
            (persisted.message_id, handler.thread_id),
        ).fetchone()
        stored = {
            "id": row["id"], "seq": int(row["seq"]), "created_at": row["created_at"],
            "metadata": metadata_object(row["metadata"]),
        }
    try:
        from registry.observer import observe_record
        from registry.registration_writer import RegistrationOrigin

        observation = observe_record(
            handler.thread_id,
            "tool",
            full_result,
            stored["metadata"],
            origin=RegistrationOrigin(
                handler.thread_id, handler.turn_id, "native_observer"
            ),
        )
    except Exception as exc:
        from loguru import logger

        logger.warning(f"registration observer failed: {exc}")
        observation = None
    return observation, stored


def _settle_declared_call(handler: Any, payload: dict, full_result: Any) -> dict:
    """Record the result on its declared call and return that call."""
    for tool_call in handler.tool_calls:
        if tool_call["id"] == payload["id"]:
            tool_call["result"] = full_result
            return tool_call
    raise RuntimeError(
        f"tool result {payload['id']!r} is not declared by the active Assistant Step"
    )


async def handle_native_tool_complete(
    handler: Any,
    payload: dict,
    persistence: tuple[Any, Any] = (run_in_db, write_transaction),
) -> Any:
    """Persist one declared native tool result and return its portable row."""
    run_in_db_fn, write_transaction_fn = persistence
    if handler.tool_step_id is None:
        raise RuntimeError("tool result has no active Assistant Step")
    full_result = payload.get("result", "") or payload.get("result_preview", "")
    declared = _settle_declared_call(handler, payload, full_result)

    media_metadata = _media_metadata(full_result)
    observation, stored = await run_in_db_fn(
        _persist_tool_row,
        handler,
        write_transaction_fn=write_transaction_fn,
        payload=payload,
        full_result=full_result,
        media_metadata=media_metadata,
    )
    tool_meta = stored["metadata"]
    if observation and observation.artifact:
        from registry.service import notify_changed

        notify_changed(observation.artifact)
    if observation and observation.activity:
        from doceditor.presence import publish_document_activity

        publish_document_activity(observation.activity)
    # The finalized Step frame reads the declared call, so it gets the same row.
    settled = SettledResult.from_row(full_result, tool_meta)
    display = project_tool_display(payload["name"], declared.get("args"), settled)
    declared.update(display.fields())
    # The same projection of the row the result write just stored on the Step, so the finalized
    # frame carries what a reload reads without reading it back; a later open replaces it.
    if display.browser_chip is not None:
        handler.tool_step_browser_chip = {"browser_chip": display.browser_chip}
    await handler._emit(tool_done_frame(
        handler.thread_id, payload["id"], payload["name"], settled, full_result, display,
    ))

    from history.types import PortableMessage

    return PortableMessage(
        message_id=stored["id"],
        seq=stored["seq"],
        turn_id=handler.turn_id,
        role="tool",
        content=full_result,
        created_at=stored["created_at"],
        assistant_step_id=handler.tool_step_id,
        tool_call_id=payload["id"],
        tool_name=payload["name"],
        is_error=tool_meta.get("is_error") is True,
        synthetic_tool_result=tool_meta.get("synthetic_tool_result") is True,
        synthetic_reason=tool_meta.get("synthetic_reason"),
        content_sha256=tool_meta["tool_result_sha256"],
        content_byte_count=tool_meta["tool_result_byte_count"],
        media_metadata=media_metadata,
    )
