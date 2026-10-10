"""What keeps a model-switch destination the same while the switch is in flight."""

from __future__ import annotations

from thread.model_state import ModelSwitchPreparationError

#: What names the destination a switch was asked for. Catalog discovery
#: refreshes every other resolved fact (window, label, capabilities,
#: availability) while that choice stands. ``account_id`` is not here: the
#: resolver never sets it, and the thread's account binding is its own record.
DESTINATION_IDENTITY = ("catalog_id", "harness", "provider", "backend", "transport_model")


def destination_identity_changes(prepared, current) -> list[str]:
    """Name the identity fields on which two resolutions disagree."""
    changed = [
        name for name in DESTINATION_IDENTITY
        if getattr(prepared, name, None) != getattr(current, name, None)
    ]
    # A selector that now resolves to another model is another destination;
    # an unreported resolution is no evidence either way.
    before, after = (getattr(target, "expected_reported_model", None) for target in (prepared, current))
    if before and after and before != after:
        changed.append("expected_reported_model")
    return changed


def refreshed_fields(prepared, current) -> list[str]:
    """Name every resolved fact that differs, identity or not."""
    from dataclasses import fields

    from llm.resolution import ExecutionTarget

    return [
        field.name for field in fields(ExecutionTarget)
        if getattr(prepared, field.name, None) != getattr(current, field.name, None)
    ]


def field_changes(prepared, current, names) -> str:
    """Render ``name=old->new`` for the log line that names a change."""
    return " ".join(
        f"{name}={getattr(prepared, name, None)!r}->{getattr(current, name, None)!r}"
        for name in names
    )


def require_dispatchable_destination(target) -> None:
    """Fail a destination that cannot take the switch, with its reason."""
    if not target.available:
        reason = getattr(target, "availability_reason", None) or "destination unavailable"
        raise ModelSwitchPreparationError(
            f"model switch destination is unavailable: {reason}"
        )
    window = target.context_window
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ModelSwitchPreparationError(
            "model switch destination has no exact context window"
        )
