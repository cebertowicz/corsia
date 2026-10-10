"""Settle the exact tool lineage owned by a Turn at its terminal boundary."""

from __future__ import annotations

import hashlib
import uuid

from loguru import logger

from core._tool_result_delivery_support import settled_or_debt
from core.tool_result_debt import (
    UnsettledToolCall,
    declared_call_id,
    declared_tool_arguments,
    declared_tool_name,
    read_declaration_metadata,
    readable_call_id,
)
from core.tool_result_binding import quarantine_stranded_tool_results
from core.tool_result_delivery import TURN_ENDED_WITHOUT_RESULT_REASON
from core.tool_result_projection import tool_result_integrity_metadata
from core.turns_steps import MISSING_TOOL_RESULT_TEXT
from infra.delivery_states import TOOL_RESULT_BOUND
from infra.queries import insert_message


_SETTLEMENT_NAMESPACE = uuid.UUID("2c1de8f9-661b-4ed6-b5be-66fb4b91b1da")


def _step_declared_calls(
    row, thread_id: str, turn_id: str,
) -> list[UnsettledToolCall]:
    """Read one Assistant Step's identifiable calls without vetoing terminal."""
    metadata = read_declaration_metadata(row["metadata"])
    if metadata is None:
        return []
    calls = metadata.get("llm_tool_calls") or metadata.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    declared: list[UnsettledToolCall] = []
    for call in calls:
        call_id = declared_call_id(call)
        if call_id is None:
            continue
        declared.append(UnsettledToolCall(
            thread_id=thread_id,
            turn_id=turn_id,
            assistant_step_id=str(row["id"]),
            call_id=call_id,
            name=declared_tool_name(call),
            arguments=declared_tool_arguments(call),
        ))
    return declared


def _turn_declared_calls(
    conn, thread_id: str, turn_id: str,
) -> list[UnsettledToolCall]:
    """Read only the calls declared by this Turn, using its indexed lineage."""
    declared: list[UnsettledToolCall] = []
    rows = conn.execute(
        "SELECT id,metadata FROM messages WHERE thread_id=? AND role='assistant' "
        "AND json_valid(metadata) AND json_extract(metadata,'$.turn_id')=?",
        (thread_id, turn_id),
    ).fetchall()
    for row in rows:
        try:
            declared.extend(_step_declared_calls(row, thread_id, turn_id))
        except Exception as exc:  # noqa: BLE001 - one Step never vetoes terminal
            logger.error(
                "turn {} on {}: Assistant Step {!r} declaration is unreadable "
                "({}: {}); its calls stay unpaid debt",
                turn_id, thread_id, row["id"], type(exc).__name__, exc,
            )
    return declared


def _is_answered(conn, debt: UnsettledToolCall) -> bool:
    """Return whether a result is bound to this exact Step and call."""
    call_id = debt.call_id.encode("utf-8", "surrogatepass")
    return any(
        conn.execute(
            "SELECT 1 FROM messages WHERE thread_id=? AND role='tool' "
            "AND json_valid(metadata) "
            "AND json_extract(metadata,'$.assistant_step_id') IS ? "
            "AND json_extract(metadata,'$.tool_call_id')=CAST(? AS TEXT) LIMIT 1",
            (debt.thread_id, step_id, call_id),
        ).fetchone() is not None
        for step_id in (debt.assistant_step_id, None)
    )


def _write_authored_result(
    conn, thread_id: str, turn_id: str, debt: UnsettledToolCall,
) -> None:
    """Write CORSIA's deterministic missing-result row for one owed call."""
    identity = f"{debt.assistant_step_id}::{debt.call_id}::missing"
    digest = hashlib.sha256(
        identity.encode("utf-8", "surrogatepass")
    ).hexdigest()
    insert_message(
        thread_id,
        "tool",
        MISSING_TOOL_RESULT_TEXT,
        {
            "turn_id": turn_id,
            "assistant_step_id": debt.assistant_step_id,
            "tool_call_id": debt.call_id,
            "provider_tool_call_id": debt.call_id,
            "tool_invocation_id": identity,
            "tool_result_binding_state": TOOL_RESULT_BOUND,
            "name": debt.name,
            "arguments": debt.arguments,
            "is_error": True,
            "synthetic_tool_result": True,
            "synthetic_reason": TURN_ENDED_WITHOUT_RESULT_REASON,
            **tool_result_integrity_metadata(MISSING_TOOL_RESULT_TEXT),
        },
        msg_id=str(uuid.uuid5(_SETTLEMENT_NAMESPACE, digest)),
        db=conn,
    )


def _pay_turn_debt(conn, thread_id: str, turn_id: str) -> int:
    """Persist one missing result for each unanswered call this Turn declared."""
    settled = 0
    for debt in _turn_declared_calls(conn, thread_id, turn_id):
        label = (
            f"tool call {readable_call_id(debt.call_id)} of turn {turn_id} on "
            f"{thread_id} (Assistant Step {debt.assistant_step_id})"
        )
        with settled_or_debt(conn, label=label):
            if _is_answered(conn, debt):
                continue
            _write_authored_result(conn, thread_id, turn_id, debt)
            settled += 1
    return settled


def settle_turn_tool_lineage(conn, turn_id: str) -> None:
    """Settle this Turn's calls and provisional results without vetoing terminal."""
    owner = conn.execute(
        "SELECT thread_id FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone()
    if owner is None:
        return
    thread_id = str(owner["thread_id"])
    settled = 0
    with settled_or_debt(
        conn, label=f"declared calls of turn {turn_id} on {thread_id}",
    ):
        settled = _pay_turn_debt(conn, thread_id, turn_id)
    stranded = 0
    with settled_or_debt(
        conn, label=f"stranded rows of turn {turn_id} on {thread_id}",
    ):
        stranded = quarantine_stranded_tool_results(
            thread_id=thread_id,
            db=conn,
            owning_turn_id=turn_id,
        )
    if settled or stranded:
        logger.warning(
            "turn {} terminal settled {} unanswered call(s) and quarantined "
            "{} unbindable result(s) on {}",
            turn_id, settled, stranded, thread_id,
        )
