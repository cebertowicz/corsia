"""Validate the exact model IDs saved for the reviewer settings."""

from __future__ import annotations


def reviewer_model_error(model_id: object) -> str | None:
    """Validate an exact catalog ID against the reviewer's supported harnesses."""
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    missing = "That model isn't in the catalog. Choose another model."
    if not isinstance(model_id, str):
        return missing
    try:
        target = resolve_execution_target(model_id)
    except (ModelUnavailableError, ValueError):
        return missing
    if target.catalog_id != model_id:
        return missing
    if target.harness not in ("codex", "claude-code"):
        return "The reviewer needs a Codex or Claude Code model. Choose another model."
    return None
