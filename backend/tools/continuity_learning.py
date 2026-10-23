"""Correction-learning operations carried by ``continuity_edit``."""

from __future__ import annotations

import json
from typing import Any


def live_operation(db: Any, thread_id: str, operation: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Record immutable learning facts, or leave another operation to continuity."""
    arguments = {key: value for key, value in params.items() if key != "operation"}
    if operation == "triage":
        from core.correction_learning import record_triage

        return {"operation": operation, **record_triage(db, thread_id=thread_id, **arguments)}
    if operation == "outcome":
        from core.correction_learning import record_terminal_outcome

        return {
            "operation": operation,
            **record_terminal_outcome(db, thread_id=thread_id, **arguments),
        }
    return None


def result_text(result: dict[str, Any]) -> str | None:
    """Render the structured result for correction-learning operations."""
    if result.get("operation") in {"triage", "outcome"}:
        return json.dumps(result, indent=2, sort_keys=True)
    return None


def execution_error(operation: Any, params: dict[str, Any]) -> str | None:
    """Refuse learning facts that omit their immutable correction subject."""
    if operation in {"triage", "outcome"} and not str(params.get("subject_key") or "").strip():
        return f"Error: {operation} requires subject_key"
    return None
