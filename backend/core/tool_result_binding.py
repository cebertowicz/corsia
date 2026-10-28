"""Provider-echo binding and quarantine for canonical tool results."""

from __future__ import annotations

import json

import core._tool_result_delivery_support as delivery_support
from core.tool_result_echo_matching import ranked_echo_matches as _ranked_echo_matches
from core.tool_result_invocation import invocation_reference_from_echo
from infra.delivery_states import (
    TOOL_RESULT_BOUND,
    TOOL_RESULT_PROVISIONAL,
    TOOL_RESULT_QUARANTINED_UNBOUND,
)

_BindingError = delivery_support.ToolResultBindingError


# Matching lives alongside the shared echo identity rules.


def _cas_bind_echo(
    db,
    *,
    row,
    result: delivery_support.ToolExecutionResult,
    thread_id: str,
    provider_tool_call_id: str,
) -> delivery_support.ToolExecutionResult:
    metadata = delivery_support.metadata_object(row["metadata"])
    updated_metadata = dict(metadata)
    updated_metadata["tool_call_id"] = provider_tool_call_id
    updated_metadata["provider_tool_call_id"] = provider_tool_call_id
    updated_metadata["tool_result_binding_state"] = TOOL_RESULT_BOUND
    updated = db.execute(
        "UPDATE messages SET metadata=? WHERE id=? AND thread_id=? "
        "AND role='tool' AND metadata=?",
        (
            json.dumps(updated_metadata),
            result.message_id,
            thread_id,
            row["metadata"],
        ),
    )
    rebound = delivery_support.existing_result(db, result.message_id)
    if rebound is None or rebound.provider_tool_call_id != provider_tool_call_id:
        raise _BindingError("tool-result echo CAS binding lost ownership")
    if updated.rowcount not in (0, 1):
        raise _BindingError("tool-result echo CAS updated multiple rows")
    return rebound


def _bind_undispatched_echo(
    db, *, thread_id: str, assistant_step_id: str,
    provider_tool_call_id: str, echoed_content: str,
) -> delivery_support.ToolExecutionResult | None:
    """Zero rows for a declared call: on the live (non-checkpoint) Turn path
    ``execute_tool`` persists bytes before returning, so this echo can only
    mean the call never ran there."""
    step = db.execute(
        "SELECT metadata FROM messages WHERE id=? AND thread_id=? AND role='assistant'",
        (assistant_step_id, thread_id),
    ).fetchone()
    if step is None:
        return None
    metadata = delivery_support.metadata_object(step["metadata"])
    declared = None
    for call in delivery_support.declared_calls(metadata):
        call_id, name, _arguments = delivery_support.call_parts(call)
        if call_id == provider_tool_call_id:
            declared = name
            break
    if declared is None:
        return None
    turn_id = delivery_support.required_text(
        metadata.get("turn_id"),
        "Assistant Step Turn identity",
    )
    from core.tool_result_delivery import (
        persist_tool_execution_result,
        synthetic_result_identity,
    )
    from infra.delivery_states import UNDISPATCHED_TOOL_REASON

    invocation_id, extra_metadata = synthetic_result_identity(
        tool_use_id=provider_tool_call_id,
        reason=UNDISPATCHED_TOOL_REASON,
    )
    return persist_tool_execution_result(
        thread_id=thread_id,
        turn_id=turn_id,
        invocation_id=invocation_id,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_tool_call_id,
        name=declared,
        arguments=None,
        content=echoed_content,
        is_error=True,
        extra_metadata=extra_metadata,
        db=db,
    )


def _bind_echo(*, db, thread_id: str, provider_tool_call_id: str, assistant_step_id: str,
               echoed_content: str) -> delivery_support.ToolExecutionResult:
    rows = db.execute(
        "SELECT id,content,metadata FROM messages "
        "WHERE thread_id=? AND role='tool' AND json_valid(metadata) "
        "AND json_extract(metadata,'$.assistant_step_id')=?",
        (thread_id, assistant_step_id),
    ).fetchall()
    marker_present, invocation_id = invocation_reference_from_echo(echoed_content)
    if marker_present and invocation_id is None:
        raise _BindingError(
            "provider tool-result echo does not identify one canonical result"
        )
    matches = _ranked_echo_matches(
        rows,
        invocation_id=invocation_id,
        provider_tool_call_id=provider_tool_call_id,
    )
    if len(matches) != 1:
        if (
            not marker_present
            and not rows
            and (bound := _bind_undispatched_echo(
                db, thread_id=thread_id, assistant_step_id=assistant_step_id,
                provider_tool_call_id=provider_tool_call_id,
                echoed_content=echoed_content,
            )) is not None
        ):
            return bound
        raise _BindingError(
            "provider tool-result echo does not identify one canonical result"
        )
    row = matches[0]
    result = delivery_support.result_from_row(row)
    if result.provider_tool_call_id is not None:
        if result.provider_tool_call_id != provider_tool_call_id:
            raise _BindingError(
                "tool result is already bound to a different provider call"
            )
        return result
    metadata = delivery_support.metadata_object(row["metadata"])
    provider_call, _arguments = delivery_support.select_provider_call(
        db=db,
        thread_id=thread_id,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_tool_call_id,
        name=result.name,
        arguments=metadata.get("arguments"),
    )
    if provider_call != provider_tool_call_id:
        raise _BindingError("provider echo does not match declared call")
    conflict = db.execute(
        "SELECT id FROM messages WHERE thread_id=? AND role='tool' "
        "AND json_valid(metadata) AND json_extract(metadata,'$.assistant_step_id')=? "
        "AND json_extract(metadata,'$.tool_call_id')=? AND id<>?",
        (thread_id, assistant_step_id, provider_tool_call_id, result.message_id),
    ).fetchone()
    if conflict is not None:
        raise _BindingError(
            "provider tool call is already bound to a different canonical result"
        )
    return _cas_bind_echo(
        db,
        row=row,
        result=result,
        thread_id=thread_id,
        provider_tool_call_id=provider_tool_call_id,
    )


def read_bound_tool_result(
    *,
    thread_id: str,
    provider_tool_call_id: str,
    db=None,
) -> delivery_support.ToolExecutionResult | None:
    """Read the canonical result bound to an exact provider call."""
    thread_id = delivery_support.required_text(thread_id, "thread identity")
    provider_tool_call_id = delivery_support.required_text(
        provider_tool_call_id,
        "provider tool call identity",
    )
    if db is None:
        from infra.database import get_db

        owned_db = get_db()
        try:
            return read_bound_tool_result(
                thread_id=thread_id,
                provider_tool_call_id=provider_tool_call_id,
                db=owned_db,
            )
        finally:
            owned_db.close()
    rows = db.execute(
        "SELECT id,content,metadata FROM messages "
        "WHERE thread_id=? AND role='tool' AND json_valid(metadata) "
        "AND json_extract(metadata,'$.tool_call_id')=?",
        (thread_id, provider_tool_call_id),
    ).fetchall()
    if len(rows) > 1:
        raise _BindingError("provider tool call has multiple canonical result rows")
    return delivery_support.result_from_row(rows[0]) if rows else None


def quarantine_unbound_tool_result(
    *,
    thread_id: str,
    message_id: str,
    db,
) -> delivery_support.ToolExecutionResult:
    """CAS-mark one provisional result unsafe until its provider echo arrives."""
    thread_id = delivery_support.required_text(thread_id, "thread identity")
    message_id = delivery_support.required_text(
        message_id,
        "tool-result message identity",
    )
    row = db.execute(
        "SELECT id,content,metadata FROM messages "
        "WHERE id=? AND thread_id=? AND role='tool'",
        (message_id, thread_id),
    ).fetchone()
    if row is None:
        raise _BindingError("provisional tool result does not exist")
    result = delivery_support.result_from_row(row)
    if result.provider_tool_call_id is not None:
        raise _BindingError("bound tool result cannot become unbound")
    metadata = delivery_support.metadata_object(row["metadata"])
    state = metadata.get("tool_result_binding_state")
    if state == TOOL_RESULT_QUARANTINED_UNBOUND:
        return result
    if state != TOOL_RESULT_PROVISIONAL:
        raise _BindingError("tool result lacks provisional binding ownership")
    updated_metadata = dict(metadata)
    updated_metadata["tool_result_binding_state"] = TOOL_RESULT_QUARANTINED_UNBOUND
    updated = db.execute(
        "UPDATE messages SET metadata=? WHERE id=? AND thread_id=? "
        "AND role='tool' AND metadata=?",
        (json.dumps(updated_metadata), message_id, thread_id, row["metadata"]),
    )
    if updated.rowcount != 1:
        raise _BindingError("unbound tool-result quarantine lost CAS ownership")
    quarantined = delivery_support.existing_result(db, message_id)
    if quarantined is None or quarantined.provider_tool_call_id is not None:
        raise _BindingError("unbound tool-result quarantine was not durable")
    return quarantined


def bind_tool_result_echo(*, thread_id: str, provider_tool_call_id: str, assistant_step_id: str,
                          echoed_content: str, db=None) -> delivery_support.ToolExecutionResult:
    """Bind a provider echo, authoring a proven-undispatched result if needed."""
    thread_id = delivery_support.required_text(thread_id, "thread identity")
    provider_tool_call_id = delivery_support.required_text(provider_tool_call_id, "provider tool call identity")
    assistant_step_id = delivery_support.required_text(assistant_step_id, "Assistant Step predecessor")
    from infra.database import write_transaction

    with write_transaction(db, scope="bind_tool_result_echo") as connection:
        return _bind_echo(
            db=connection,
            thread_id=thread_id,
            provider_tool_call_id=provider_tool_call_id,
            assistant_step_id=assistant_step_id,
            echoed_content=echoed_content,
        )


_STRANDED_PROVISIONAL_ROWS = (
    "SELECT id, content, metadata FROM messages "
    "WHERE thread_id=? AND role='tool' AND json_valid(metadata) "
    "AND json_extract(metadata,'$.tool_result_binding_state')=?"
)
_STRANDED_ROWS_OF_ONE_TURN = " AND json_extract(metadata,'$.turn_id')=?"


def quarantine_stranded_tool_results(
    *,
    thread_id: str,
    db,
    owning_turn_id: str | None = None,
) -> int:
    """Quarantine provisional results whose owning Turn is terminal."""
    query = _STRANDED_PROVISIONAL_ROWS
    params: tuple[str, ...] = (thread_id, TOOL_RESULT_PROVISIONAL)
    if owning_turn_id is not None:
        query += _STRANDED_ROWS_OF_ONE_TURN
        params += (owning_turn_id,)
    rows = db.execute(query, params).fetchall()
    quarantined = 0
    for row in rows:
        with delivery_support.settled_or_debt(
            db,
            label=f"stranded tool row {row['id']!r}",
        ):
            quarantined += _quarantine_if_stranded(
                row,
                thread_id=thread_id,
                db=db,
            )
    return quarantined


def _quarantine_if_stranded(row, *, thread_id: str, db) -> int:
    """Quarantine one provisional row if its owning Turn can no longer bind it."""
    result = delivery_support.result_from_row(row)
    if result.provider_tool_call_id is not None:
        return 0
    metadata = delivery_support.metadata_object(row["metadata"])
    owner_turn_id = str(metadata.get("turn_id") or "")
    if owner_turn_id:
        turn = db.execute(
            "SELECT status FROM active_turns WHERE id=?",
            (owner_turn_id,),
        ).fetchone()
        if turn is not None and str(turn["status"]) == "running":
            return 0
    else:
        active = db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id=? "
            "AND status='running' LIMIT 1",
            (thread_id,),
        ).fetchone()
        if active is not None:
            return 0
    quarantine_unbound_tool_result(thread_id=thread_id, message_id=result.message_id, db=db)
    return 1


def metadata_object(raw: object) -> dict:
    """Public strict metadata parser for read-only tool-result consumers."""
    try:
        return delivery_support.metadata_object(raw)
    except delivery_support.ToolResultBindingError as exc:
        raise ValueError(str(exc)) from None


def canonical_message_id(thread_id: str, assistant_step_id: str, invocation_id: str) -> str:
    """Public canonical result-row identity for read-only report consumers."""
    return delivery_support.message_id(thread_id, assistant_step_id, invocation_id)


def select_provider_call_from_metadata(*, metadata: object, assistant_step_id: str,
                                       provider_tool_call_id: str, name: str,
                                       arguments: dict) -> tuple[str | None, dict]:
    """Public strict predecessor selector for read-only report consumers."""
    try:
        return delivery_support.select_provider_call_from_metadata(
            metadata=metadata, assistant_step_id=assistant_step_id,
            provider_tool_call_id=provider_tool_call_id, name=name, arguments=arguments,
        )
    except delivery_support.ToolResultBindingError as exc:
        raise ValueError(str(exc)) from None
