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
from core.tool_result_binding import quarantine_unbound_tool_result
from core.tool_result_projection import tool_result_integrity_metadata
from core.turns_steps import MISSING_TOOL_RESULT_TEXT
from infra.delivery_states import (
    TOOL_RESULT_BOUND,
    TOOL_RESULT_PROVISIONAL,
    TURN_ENDED_WITHOUT_RESULT_REASON,
)
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


def _pay_turn_debt(
    conn, thread_id: str, turn_id: str, declared: list[UnsettledToolCall],
) -> int:
    """Persist one missing result for each unanswered call this Turn declared."""
    settled = 0
    for debt in declared:
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


_STEP_PROVISIONAL_ROWS = (
    "SELECT id FROM messages WHERE thread_id=? AND role='tool' "
    "AND json_valid(metadata) AND json_extract(metadata,'$.assistant_step_id')=? "
    "AND json_extract(metadata,'$.tool_result_binding_state')=?"
)


def _quarantine_provisional_results(
    conn, thread_id: str, step_ids: set[str],
) -> int:
    """Set aside the ending Turn's provisional rows so a late echo binds its result.

    A provisional row left beside the terminal's result would win a later
    echo's ranking and then meet that result as a second row for the same call.
    Each Step is one seek on idx_messages_tool_lineage.
    """
    quarantined = 0
    for step_id in sorted(step_ids):
        rows = conn.execute(
            _STEP_PROVISIONAL_ROWS, (thread_id, step_id, TOOL_RESULT_PROVISIONAL),
        ).fetchall()
        for row in rows:
            with settled_or_debt(conn, label=f"provisional tool row {row['id']!r}"):
                quarantine_unbound_tool_result(
                    thread_id=thread_id, message_id=str(row["id"]), db=conn,
                )
                quarantined += 1
    return quarantined


def settle_turn_tool_lineage(conn, thread_id: str, turn_id: str) -> None:
    """Give every call this Turn declared a durable result, never vetoing terminal.

    Harnesses settle their own calls while their process is alive. This is the
    one shared settlement for a Turn whose process is gone (restart recovery)
    or whose harness left a call open. Without a durable row the call stays
    debt, and every later cold checkpoint over it is refused. The Turn's
    provisional rows are quarantined in the same pass, so a late echo binds
    the result written here.
    """
    settled = quarantined = 0
    with settled_or_debt(
        conn, label=f"declared calls of turn {turn_id} on {thread_id}",
    ):
        declared = _turn_declared_calls(conn, thread_id, turn_id)
        settled = _pay_turn_debt(conn, thread_id, turn_id, declared)
        quarantined = _quarantine_provisional_results(
            conn, thread_id, {debt.assistant_step_id for debt in declared},
        )
    if settled or quarantined:
        logger.warning(
            "turn {} terminal settled {} unanswered call(s) and quarantined "
            "{} unbound result(s) on {}",
            turn_id, settled, quarantined, thread_id,
        )
