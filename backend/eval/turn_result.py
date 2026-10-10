"""Project one canonical Turn's durable result for the eval response.

A canonical Native Turn persists every Step through
``core.step_handler.StepHandler`` while ``ws.turn.execute_turn`` owns the step
callbacks, so the eval route cannot also observe raw step payloads. This module
writes nothing: it reads the state the canonical path already recorded — the
Turn's persisted Steps and its ``llm_usage`` rows — and maps them to the eval
response shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from infra.db_async import run_in_db

_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "total_tokens",
    "cost_usd",
)
_STEP_ROLES = ("assistant", "system")


@dataclass(frozen=True, slots=True)
class EvalTurnResult:
    """One canonical Turn's persisted result, as the eval response needs it."""

    content: str | None
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    error: str | None


def _read_steps(thread_id: str, turn_id: str) -> list[dict[str, Any]]:
    """Read the Turn's own assistant and system Step rows in transcript order."""
    from infra.database import get_db

    marks = ", ".join("?" for _ in _STEP_ROLES)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT role, content, metadata FROM messages "
            f"WHERE thread_id=? AND role IN ({marks}) "
            "AND json_extract(metadata, '$.turn_id')=? ORDER BY seq",
            (thread_id, *_STEP_ROLES, turn_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def _read_usage(thread_id: str, turn_id: str) -> list[dict[str, Any]]:
    """Read the Turn's canonical provider-call ledger rows."""
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT " + ", ".join(_USAGE_FIELDS) + " FROM llm_usage "
            "WHERE thread_id=? AND turn_id=? ORDER BY rowid",
            (thread_id, turn_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def _step_metadata(row: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(row.get("metadata") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _measured_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the Turn's calls; an unmeasured field stays absent, never invented."""
    measured: dict[str, Any] = {}
    for row in rows:
        for field in _USAGE_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            measured[field] = (measured.get(field) or 0) + value
    return measured


def _project_step(row: dict[str, Any], collected: dict[str, Any]) -> None:
    """Fold one persisted Step into the response's text, tool calls and error."""
    metadata = _step_metadata(row)
    if row.get("role") == "system":
        if metadata.get("type") == "llm_error" and row.get("content"):
            collected["error"] = row["content"]
        return
    if metadata.get("step_type") == "text" and row.get("content"):
        collected["text"].append(row["content"])
    for call in metadata.get("llm_tool_calls") or []:
        function = call.get("function") or {}
        collected["tool_calls"].append({
            "id": call.get("id"),
            "name": function.get("name"),
            "arguments": function.get("arguments"),
        })


def _assemble(
    steps: list[dict[str, Any]], usage: list[dict[str, Any]],
) -> EvalTurnResult:
    collected: dict[str, Any] = {"text": [], "tool_calls": [], "error": None}
    for row in steps:
        _project_step(row, collected)
    return EvalTurnResult(
        content="".join(collected["text"]) or None,
        tool_calls=collected["tool_calls"],
        usage=_measured_usage(usage),
        error=collected["error"],
    )


async def read_turn_result(thread_id: str, turn_id: str) -> EvalTurnResult:
    """Read one exact Turn's persisted Steps and usage into the response shape."""
    steps = await run_in_db(_read_steps, thread_id, turn_id)
    usage = await run_in_db(_read_usage, thread_id, turn_id)
    return _assemble(steps, usage)


__all__ = ["EvalTurnResult", "read_turn_result"]