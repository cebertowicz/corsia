"""Discover missing model context-window evidence and notify catalog consumers."""

from __future__ import annotations

from loguru import logger

from llm.context_window import _catalog_row, get_context_window_or_none


async def run_discovery_owner(model: str, row: dict | None) -> None:
    """Run the exact discovery owner for one catalog row, if it has one.

    Harness rows (Claude Code, Codex) are refreshed by their connector; network
    and external rows by their server's discovery. Cloud rows have no live
    discovery endpoint and no owner to run.
    """
    data = row or {}
    harness = str(data.get("harness") or "native")
    discovered = False
    if harness in {"claude-code", "codex"}:
        if not (data.get("enabled") and data.get("available")):
            return
        from connections.harness_models import refresh_provider

        await refresh_provider(harness, reason="missing_context_evidence")
        discovered = True
    else:
        litellm_model = str(data.get("litellm_model") or model)
        provider, sep, rest = litellm_model.partition("@")
        server_id = rest.split("/", 1)[0].strip() if sep else ""
        if not server_id:
            return
        if provider == "omlx":
            from llm.catalog_network_models import discover_network_models

            await discover_network_models(server_id)
            discovered = True
        elif provider == "external":
            from llm.external_provider import discover_external_models

            await discover_external_models(server_id)
            discovered = True
    if discovered:
        from infra.state.ws import notify_models_changed

        notify_models_changed()


async def rediscover_context_window(model: str) -> int | None:
    """Re-enter discovery for ``model`` and return its freshly resolved window."""
    row = None
    try:
        row = _catalog_row(model)
    except Exception as exc:
        logger.warning("model catalog lookup failed for {}: {}", model, exc)
    try:
        await run_discovery_owner(model, row)
    except Exception as exc:
        logger.warning("context-window rediscovery failed for {}: {}", model, exc)
    return get_context_window_or_none(model)


async def ensure_context_window(model: str) -> int | None:
    """Resolve a live window, re-entering discovery once when it is missing."""
    window = get_context_window_or_none(model)
    if window:
        return window
    return await rediscover_context_window(model)
