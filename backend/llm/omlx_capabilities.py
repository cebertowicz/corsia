"""Reasoning capability as an oMLX server reports it.

oMLX reads each model's chat template at discovery and reports what that
template actually accepts. Reading those fields off its payloads is a distinct
concern from the catalog plumbing that stores them, so it lives here.

A field the server omits means "this server cannot say", which is never the
same as it saying the model has no such capability. Absent stays absent.
``reasoning_efforts`` (oMLX 0.7.1rc1+) is the template's own vocabulary; None
means it names none, so the picker falls back to the on/off toggle.
``reasoning_effort_options`` is oMLX's generic custom-control list, trusted only
where ``reasoning_efforts`` is present and empty (see ``reasoning_config``).
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


def apply_admin_model(canonical: dict[str, dict], model: dict) -> None:
    """Enrich a discovered model with the admin endpoint's fields."""
    from llm.model_metadata import extract_context_length

    provider_model_id = model.get("id")
    if not provider_model_id or provider_model_id not in canonical:
        return
    settings = model.get("settings") or {}
    alias = (settings.get("model_alias") or "").strip()
    if alias:
        canonical[provider_model_id]["model_alias"] = alias
    capture_configured(settings, canonical[provider_model_id])
    capture_reported(model, canonical[provider_model_id])
    if isinstance(model.get("config_model_type"), str):
        canonical[provider_model_id]["config_model_type"] = model["config_model_type"]
    if canonical[provider_model_id].get("context_length") is not None:
        return
    window = extract_context_length(model)
    if window is not None:
        canonical[provider_model_id]["context_length"] = window


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
