"""Durable binding for tool results retained after their effect completed."""

from __future__ import annotations

import sqlite3

from infra.database import write_transaction
from infra.tool_result_errors import is_error_result


def _completed_binding(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    turn_id: str,
    completed,
) -> tuple[dict, str | None, str, str]:
    """Require one retained result's captured invocation and Assistant Step."""
    from core import _tool_result_delivery_support as delivery_support

    if completed.thread_id not in (None, thread_id) or completed.turn_id not in (
        None, turn_id,
    ):
        raise delivery_support.ToolResultBindingError(
            "completed tool result does not belong to the terminal Turn"
        )
    if completed.fork_id is not None:
        raise delivery_support.ToolResultBindingError(
            "fork-private tool result cannot bind to a foreground Turn"
        )
    arguments = completed.arguments
    if not isinstance(arguments, dict):
        raise delivery_support.ToolResultBindingError(
            "completed tool result lacks exact invocation arguments"
        )
    provider_call_id = completed.provider_tool_call_id
    invocation_id = completed.invocation_id or provider_call_id
    if not invocation_id:
        raise delivery_support.ToolResultBindingError(
            "completed tool result lacks exact invocation identity"
        )
    assistant_step_id = completed.assistant_step_id
    if assistant_step_id is None:
        raise delivery_support.ToolResultBindingError(
            "completed tool result lacks exact Assistant Step identity"
        )
    return arguments, provider_call_id, invocation_id, assistant_step_id


def _existing_durable_result(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    turn_id: str,
    completed,
    arguments: dict,
    provider_call_id: str | None,
    invocation_id: str,
    assistant_step_id: str,
) -> tuple[bool, object | None]:
    """Return an exactly equivalent durable result, refusing every conflict."""
    from core import _tool_result_delivery_support as delivery_support

    rows = db.execute(
        "SELECT id,content,metadata FROM messages WHERE thread_id=? AND role='tool' "
        "AND json_valid(metadata) AND json_extract(metadata,'$.turn_id')=? "
        "AND json_extract(metadata,'$.assistant_step_id')=? "
        "AND json_extract(metadata,'$.tool_call_id') IS ?",
        (thread_id, turn_id, assistant_step_id, provider_call_id),
    ).fetchall()
    if len(rows) > 1:
        raise delivery_support.ToolResultBindingError(
            "completed tool result has ambiguous durable bindings"
        )
    if not rows:
        return False, None
    row = rows[0]
    metadata = delivery_support.metadata_object(row["metadata"])
    canonical = completed.canonical
    message_id = getattr(canonical, "message_id", None)
    canonical_invocation = getattr(canonical, "invocation_id", None) or getattr(
        canonical, "tool_call_id", None,
    )
    stored_invocation = metadata.get("tool_invocation_id")
    invocation_matches = stored_invocation == invocation_id or (
        not stored_invocation
        and message_id == row["id"]
        and canonical_invocation == invocation_id
    )
    message_matches = message_id is None or message_id == row["id"]
    canonical_is_error = getattr(canonical, "is_error", None)
    expected_is_error = (
        canonical_is_error
        if isinstance(canonical_is_error, bool) and message_matches
        else is_error_result(completed.result)
    )
    reference = getattr(completed.result, "media", None)
    expected_media = {"media": reference.as_metadata()} if reference is not None else None
    stored_media = (
        {"media": metadata["media"]} if isinstance(metadata.get("media"), dict) else None
    )
    declared_call_id, declared_arguments = delivery_support.select_provider_call(
        db=db,
        thread_id=thread_id,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_call_id,
        name=completed.name,
        arguments=arguments,
    )
    if (
        declared_call_id != provider_call_id
        or declared_arguments != arguments
        or row["content"] != completed.result
        or metadata.get("name") != completed.name
        or not invocation_matches
        or not message_matches
        or bool(metadata.get("is_error")) is not expected_is_error
        or stored_media != expected_media
    ):
        raise delivery_support.ToolResultBindingError(
            "completed tool result conflicts with its durable binding"
        )
    return True, delivery_support.result_from_row(row)


def _persist_one_completed_tool_result(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    turn_id: str,
    completed,
) -> object:
    """Bind one retained result only to its exact current-Turn declaration."""
    from core.tool_result_delivery import persist_tool_execution_result

    arguments, provider_call_id, invocation_id, assistant_step_id = _completed_binding(
        db, thread_id=thread_id, turn_id=turn_id, completed=completed,
    )
    found, durable = _existing_durable_result(
        db,
        thread_id=thread_id,
        turn_id=turn_id,
        completed=completed,
        arguments=arguments,
        provider_call_id=provider_call_id,
        invocation_id=invocation_id,
        assistant_step_id=assistant_step_id,
    )
    if found:
        return durable
    content = completed.result
    reference = getattr(content, "media", None)
    extra_metadata = (
        {"media": reference.as_metadata()} if reference is not None else None
    )
    return persist_tool_execution_result(
        thread_id=thread_id,
        turn_id=turn_id,
        invocation_id=invocation_id,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_call_id,
        name=completed.name,
        arguments=arguments,
        content=content,
        is_error=is_error_result(content),
        extra_metadata=extra_metadata,
        db=db,
    )


def _retained_label(completed, thread_id: str, turn_id: str) -> str:
    """Name one retained unit for its debt log without its bytes or arguments."""
    from core.tool_result_debt import readable_call_id

    call_id = completed.provider_tool_call_id or completed.invocation_id
    result = completed.result
    size = f", {len(result.encode('utf-8', 'surrogatepass'))} bytes" if isinstance(result, str) else ""
    return (
        f"retained result of tool call {readable_call_id(str(call_id))} "
        f"({readable_call_id(str(completed.name))}{size}) of turn {turn_id} on {thread_id}"
    )


def persist_completed_tool_results_in_transaction(
    db: sqlite3.Connection,
    thread_id: str,
    turn_id: str,
    error: str | BaseException | None,
) -> tuple[object, ...]:
    """Persist known post-effect bytes before the exact Turn can go terminal.

    Each retained result is one unit of settlement: a refused unit is rolled
    back, logged by name and omitted, so only the persisted results return and
    the terminal still applies. Its call is then closed by the read-time
    missing-result stub.
    """
    from core._tool_result_delivery_support import settled_or_debt
    from tools.recorded_call import CompletedToolFailure

    if not isinstance(error, CompletedToolFailure):
        return ()
    persisted: list[object] = []
    for completed in error.completed:
        with settled_or_debt(db, label=_retained_label(completed, thread_id, turn_id)):
            persisted.append(_persist_one_completed_tool_result(
                db, thread_id=thread_id, turn_id=turn_id, completed=completed,
            ))
    return tuple(persisted)


def persist_completed_tool_results(
    turn_id: str,
    error: BaseException,
    *,
    db: sqlite3.Connection | None = None,
) -> tuple[object, ...]:
    """Persist retained bytes while the exact terminal owner still owns its Turn."""
    from core import _tool_result_delivery_support as delivery_support

    with write_transaction(db, scope="persist_completed_tool_results") as connection:
        row = connection.execute(
            "SELECT thread_id,status FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise delivery_support.ToolResultBindingError(
                "completed tool result lost its exact active Turn owner"
            )
        return persist_completed_tool_results_in_transaction(
            connection, str(row["thread_id"]), turn_id, error,
        )
