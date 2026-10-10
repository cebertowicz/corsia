"""Read-only canonical tool-result validation for report projections."""
from __future__ import annotations

import json
from typing import Any

from core.lesson_report_sql import json_row_chunks
from core.tool_result_binding import (
    canonical_message_id,
    metadata_object,
    select_provider_call_from_metadata,
)
from core.tool_result_projection import tool_result_integrity_metadata
from infra.delivery_states import TOOL_RESULT_BOUND


def canonical_result_reasons(db, rows) -> dict[str, str | None]:
    """Return one canonical-lineage reason per scoped tool-result row.

    The report supplies only scoped row facts. This public tool-result-family
    reader fetches its own authoritative Assistant Step predecessors in batches.
    """
    parsed: dict[str, dict] = {}
    reasons: dict[str, str | None] = {}
    requested: dict[str, list[str]] = {}
    for row in rows:
        row_id = str(row["id"])
        try:
            metadata = metadata_object(row["metadata"])
        except ValueError:
            reasons[row_id] = "canonical invocation lineage unavailable"
            continue
        parsed[row_id] = metadata
        if metadata.get("synthetic_tool_result"):
            reasons[row_id] = "synthetic result is not a body read"
            continue
        if metadata.get("is_error"):
            reasons[row_id] = "failed result is not a body read"
            continue
        step, invocation, call = (metadata.get(key) for key in (
            "assistant_step_id", "tool_invocation_id", "tool_call_id",
        ))
        if not all(isinstance(value, str) and value for value in (step, invocation, call)):
            reasons[row_id] = "canonical invocation lineage unavailable"
        elif metadata.get("tool_result_binding_state") != TOOL_RESULT_BOUND:
            reasons[row_id] = "provider binding unavailable"
        elif row_id != canonical_message_id(str(row["thread_id"]), step, invocation):
            reasons[row_id] = "result row is not canonical"
        elif any(metadata.get(key) != value for key, value in tool_result_integrity_metadata(row["content"] or "").items()):
            reasons[row_id] = "result byte identity mismatch"
        else:
            requested.setdefault(str(row["thread_id"]), []).append(step)

    predecessors: dict[tuple[str, str], Any] = {}
    wanted = json.dumps([[thread_id, step] for thread_id, steps in requested.items() for step in dict.fromkeys(steps)])
    for predecessor in json_row_chunks(
        db, "SELECT m.id AS id,m.thread_id AS thread_id,m.metadata AS metadata FROM messages m "
        "JOIN (SELECT json_extract(value,'$[0]') AS thread_id,json_extract(value,'$[1]') AS id FROM json_each(?)) w "
        "ON m.thread_id=w.thread_id AND m.id=w.id WHERE m.role='assistant'",
        (wanted,), ("id", "thread_id", "metadata"), "thread_id,id",
    ) if requested else ():
        predecessors[(str(predecessor["thread_id"]), str(predecessor["id"]))] = predecessor

    for row in rows:
        row_id = str(row["id"])
        if row_id in reasons:
            continue
        metadata = parsed[row_id]
        predecessor = predecessors.get((str(row["thread_id"]), str(metadata["assistant_step_id"])))
        try:
            if predecessor is None or metadata_object(predecessor["metadata"]).get("turn_id") != metadata.get("turn_id"):
                reasons[row_id] = "Assistant Step Turn lineage unavailable"
            else:
                select_provider_call_from_metadata(
                    metadata=predecessor["metadata"], assistant_step_id=str(metadata["assistant_step_id"]),
                    provider_tool_call_id=str(metadata["tool_call_id"]), name=metadata["name"],
                    arguments=metadata["arguments"],
                )
                reasons[row_id] = None
        except (KeyError, ValueError):
            reasons[row_id] = "Assistant Step does not uniquely bind this result"
    return reasons
