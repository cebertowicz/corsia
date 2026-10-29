"""Common event-intake failure classification and caller-facing error copy."""
import sqlite3

_UNAVAILABLE = ("database is locked", "database is busy", "unable to open database", "disk i/o error")
_DECLARE_IT = "declare it with use: route or both in your measures.yaml and run measure_sync"
_ERROR_FIXES = {
    "facts_not_object": "Pass the facts as a JSON object.",
    "bad_json": "Pass the facts as valid JSON.",
    "too_large": "Send fewer or shorter facts.",
    "undeclared_type": f"Check the type name, or {_DECLARE_IT}.",
    "undeclared_fact": "Use only facts declared for this event type.",
    "bad_fact_kind": "Match each declared fact kind.",
    "record_only": f"To route it, {_DECLARE_IT}.",
    "not_your_namespace": f"Emit only a type you declared; to add one, {_DECLARE_IT}.",
    "reserved_type": "Emit a type you declared instead.",
    "intake_unreachable": "Retry with the same event id.",
    "internal_error": "This is a CORSIA defect and was logged; report it instead of retrying.",
    "record_refused": "Fix the record as the text says; the event was not routed.",
}


def failure_code(exc: Exception) -> str:
    """``intake_unreachable`` for a database or OS that can't be reached (retry); else ``internal_error`` (a defect)."""
    if isinstance(exc, sqlite3.OperationalError):
        unreachable = any(text in str(exc).lower() for text in _UNAVAILABLE)
    else:
        unreachable = isinstance(exc, OSError)
    return "intake_unreachable" if unreachable else "internal_error"


def format_event_error(code: str, text: str, fix: str | None = None) -> str:
    """Render the common caller-facing error copy without exposing event facts."""
    return f"{code}: {text.rstrip('.')}. {fix or _ERROR_FIXES.get(code, 'Correct the event and try again.')}"
