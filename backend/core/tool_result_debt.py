"""Read-only report of declared tool calls without durable result lineage."""

from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger

from infra.database import get_db


_TOOL_BEARING = (
    "((m.role='assistant' AND (m.metadata LIKE '%\"llm_tool_calls\"%' "
    "OR m.metadata LIKE '%\"tool_calls\"%')) OR "
    "(m.role='tool' AND m.metadata LIKE '%\"tool_call_id\"%'))"
)
_FLEET_SCOPE = "COALESCE(s.lifecycle_state,'active')='active'"


@dataclass(frozen=True)
class UnsettledToolCall:
    """One identifiable declared call with no bound result row."""

    thread_id: str
    turn_id: str | None
    assistant_step_id: str
    call_id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class UnpayableDeclaration:
    """One declared call that no result identity can answer."""

    thread_id: str
    turn_id: str | None
    assistant_step_id: str
    reason: str


@dataclass(frozen=True)
class ToolDebtReport:
    """What live history owes, and what it can never pay."""

    unsettled: tuple[UnsettledToolCall, ...]
    unpayable: tuple[UnpayableDeclaration, ...]


def read_declaration_metadata(raw: object) -> dict | None:
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def declared_call_id(call: object) -> str | None:
    """Return the scalar identity a declared call can be answered under."""
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    if isinstance(call_id, str):
        return call_id if call_id.strip() else None
    if isinstance(call_id, int) and not isinstance(call_id, bool):
        return str(call_id)
    return None


def readable_call_id(call_id: str, limit: int = 120) -> str:
    """Render a bounded, escaped call identity without raising."""
    text = repr(call_id)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… ({len(call_id)} chars)"


def declared_tool_name(call: dict) -> str:
    """Return a usable name for an owed result, never blank."""
    function = call.get("function")
    for candidate in (
        function.get("name") if isinstance(function, dict) else None,
        call.get("name"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return "tool"


def declared_tool_arguments(call: dict) -> dict:
    """Read descriptive arguments without letting their shape reject debt."""
    function = call.get("function")
    raw = (
        function.get("arguments")
        if isinstance(function, dict)
        else call.get("arguments")
    )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception as exc:  # noqa: BLE001 - metadata, never a veto
            logger.warning(
                "declared tool call has unreadable arguments ({}: {}); its "
                "owed result records none, the Step keeps the bytes verbatim",
                type(exc).__name__, exc,
            )
            return {}
    return raw if isinstance(raw, dict) else {}


def _debt_rows(
    connection, thread_id: str | None, *, start_exclusive_seq: int | None = None,
):
    """Read tool-bearing rows oldest first for one thread or the active fleet."""
    scope = "m.thread_id=?" if thread_id is not None else _FLEET_SCOPE
    parameters: tuple[object, ...] = () if thread_id is None else (thread_id,)
    if start_exclusive_seq is not None:
        if thread_id is None:
            raise ValueError("a debt floor requires one thread")
        scope += " AND m.seq>?"
        parameters += (start_exclusive_seq,)
    return connection.execute(
        "SELECT m.id,m.thread_id,m.role,m.metadata,m.seq FROM messages AS m "  # noqa: S608
        f"JOIN threads AS s ON s.id=m.thread_id "
        f"WHERE {scope} AND {_TOOL_BEARING} "
        "ORDER BY m.thread_id,COALESCE(m.seq,0),m.rowid",
        parameters,
    ).fetchall()


def _read_declaration(
    row, unpayable: list[UnpayableDeclaration],
) -> dict[tuple[str, str, str], UnsettledToolCall]:
    """Return one Step's identifiable calls and record unpayable entries."""
    thread_id, step_id = str(row["thread_id"]), str(row["id"])
    metadata = read_declaration_metadata(row["metadata"])
    if metadata is None:
        unpayable.append(UnpayableDeclaration(
            thread_id, None, step_id, "its metadata is not a JSON object",
        ))
        return {}
    turn_id = metadata.get("turn_id")
    turn_id = str(turn_id) if turn_id else None
    calls = metadata.get("llm_tool_calls") or metadata.get("tool_calls") or []
    if not isinstance(calls, list):
        unpayable.append(UnpayableDeclaration(
            thread_id, turn_id, step_id, "its declared calls are not a list",
        ))
        return {}
    identified: dict[tuple[str, str, str], UnsettledToolCall] = {}
    for call in calls:
        call_id = declared_call_id(call)
        if call_id is None:
            unpayable.append(UnpayableDeclaration(
                thread_id,
                turn_id,
                step_id,
                "one declared call carries no id a result could bind to",
            ))
            continue
        identified[(thread_id, step_id, call_id)] = UnsettledToolCall(
            thread_id=thread_id,
            turn_id=turn_id,
            assistant_step_id=step_id,
            call_id=call_id,
            name=declared_tool_name(call),
            arguments=declared_tool_arguments(call),
        )
    return identified


def _declared_without_result(
    rows,
) -> tuple[list[UnsettledToolCall], list[UnpayableDeclaration]]:
    """Pair declared calls with durable results and return what stays unpaired."""
    declared: dict[tuple[str, str, str], UnsettledToolCall] = {}
    unpayable: list[UnpayableDeclaration] = []
    satisfied: set[tuple[str, str, str]] = set()
    legacy_satisfied: set[tuple[str, str]] = set()
    for row in rows:
        thread_id = str(row["thread_id"])
        if row["role"] == "assistant":
            declared.update(_read_declaration(row, unpayable))
            continue
        metadata = read_declaration_metadata(row["metadata"])
        if metadata is None:
            continue
        answered_call_id = metadata.get("tool_call_id")
        if not answered_call_id:
            continue
        answered_step_id = metadata.get("assistant_step_id")
        if answered_step_id:
            satisfied.add(
                (thread_id, str(answered_step_id), str(answered_call_id))
            )
        else:
            legacy_satisfied.add((thread_id, str(answered_call_id)))
    debt = [
        entry
        for (thread_id, step_id, call_id), entry in declared.items()
        if (thread_id, step_id, call_id) not in satisfied
        and (thread_id, call_id) not in legacy_satisfied
    ]
    return debt, unpayable


def thread_tool_debt_through(
    thread_id: str,
    through_seq: int,
    *,
    db,
    start_exclusive_seq: int | None = None,
) -> ToolDebtReport:
    """Read unpaid declarations through a cut, optionally after an installed floor.

    Results anywhere after that floor may satisfy a declaration; later
    declarations are outside this cut's install obligation.
    """
    rows = [
        row for row in _debt_rows(
            db, thread_id, start_exclusive_seq=start_exclusive_seq,
        )
        if row["role"] != "assistant" or int(row["seq"] or 0) <= through_seq
    ]
    debt, unpayable = _declared_without_result(rows)
    return ToolDebtReport(tuple(debt), tuple(unpayable))


def tool_debt_report(*, db=None) -> ToolDebtReport:
    """Return ended live history's unpaid and unpayable declarations."""
    connection = db or get_db()
    try:
        debt, unpayable = _declared_without_result(_debt_rows(connection, None))
        running = {
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM active_turns "
                "WHERE status='running'"
            )
        }
    finally:
        if db is None:
            connection.close()
    return ToolDebtReport(
        unsettled=tuple(entry for entry in debt if entry.turn_id not in running),
        unpayable=tuple(
            entry for entry in unpayable if entry.turn_id not in running
        ),
    )


def unsettled_tool_debt(*, db=None) -> list[UnsettledToolCall]:
    """Return every ended live call with no bound result row."""
    return list(tool_debt_report(db=db).unsettled)
