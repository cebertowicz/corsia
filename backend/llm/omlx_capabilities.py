"""Reasoning capability as an oMLX server reports it.

oMLX reads each model's chat template at discovery and reports what that
template actually accepts. Reading those fields off its payloads is a distinct
concern from the catalog plumbing that stores them, so it lives here.

A field the server omits means "this server cannot say", which is never the
same as it saying the model has no such capability. Absent stays absent.
``reasoning_effort_options`` is None when the template has no strict vocabulary,
which reads the same way: no discrete picker, fall back to the on/off toggle.
"""

from __future__ import annotations

from typing import Any

# Capability the server derives from the model's own chat template.
REPORTED_FIELDS = (
    "thinking_default",
    "preserve_thinking_default",
    "reasoning_efforts",
    "reasoning_effort_options",
    "reasoning_effort_default",
)


def capture_reported(model: dict[str, Any], target: dict[str, Any]) -> None:
    """Copy reported capability into ``target``, filling only what is unset.

    Fill-only because the same model is described by two payloads: the public
    status endpoint and the richer admin one. Whichever arrives first wins, and
    a null is treated as "not reported" rather than a claim.
    """
    for field in REPORTED_FIELDS:
        value = model.get(field)
        if value is not None:
            target.setdefault(field, value)


def capture_configured(settings: dict[str, Any], target: dict[str, Any]) -> None:
    """Record what this instance is currently configured to use.

    Lets a new conversation open on the server's own setting rather than on a
    placeholder the user then has to correct.
    """
    effort = (settings.get("chat_template_kwargs") or {}).get("reasoning_effort")
    if effort is not None:
        target["configured_reasoning_effort"] = effort
    if settings.get("enable_thinking") is not None:
        target["configured_enable_thinking"] = bool(settings["enable_thinking"])


def apply_metadata(
    model: dict[str, Any],
    reasoning_shape: dict[str, Any] | None,
    metadata: dict[str, Any],
) -> None:
    """Write the resolved capability onto a catalog row's metadata."""
    if reasoning_shape is not None:
        metadata["reasoning_shape"] = reasoning_shape
    preserve_default = model.get("preserve_thinking_default")
    if isinstance(preserve_default, bool):
        metadata["preserve_thinking_default"] = preserve_default
