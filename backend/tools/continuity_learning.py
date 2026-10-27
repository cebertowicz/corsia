"""Correction-learning operations carried by ``continuity_edit``."""

from __future__ import annotations

import json
from typing import Any


def live_operation(db: Any, thread_id: str, operation: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Record immutable learning facts, or leave another operation to continuity."""
    if operation not in {"triage", "outcome"}:
        return None
    from core import correction_learning
    from infra.continuity_store import ContinuityError

    arguments = {key: value for key, value in params.items() if key != "operation"}
    record = correction_learning.record_triage if operation == "triage" else correction_learning.record_terminal_outcome
    try:
        return {"operation": operation, **record(db, thread_id=thread_id, **arguments)}
    except ValueError as exc:  # a refused field of this fact, returned to the caller to correct
        raise ContinuityError(str(exc)) from exc


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
