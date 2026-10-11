"""Model capability helpers and provider-params factory."""

from __future__ import annotations

import os
from datetime import timedelta

from infra import state
from infra.time import utc_now

# No context_window_max here: there is no safe fallback for unknown models.
_DEFAULT_CAPABILITIES = {
    "supports_vision": False,
    "supports_tools": True,
    "supports_reasoning": False,
}


def _caps_from_row(row: dict) -> dict:
    """Build a capabilities dict from a ``models`` table row."""
    from llm.context_window import resolve_window_for_row

    ctx = resolve_window_for_row(row)
    out = {
        "supports_vision": bool(row.get("supports_vision")),
        "supports_tools": bool(row.get("supports_tools", True)),
        "supports_reasoning": bool(row.get("supports_reasoning")),
    }
    if ctx:
        out["context_window_max"] = int(ctx)
    return out


def get_model_capabilities(model: str) -> dict:
    """Return capabilities dict for a model (litellm string or model ID).

    Includes ``context_window_max`` only when the catalog has a real value.
    """
    from llm.catalog import get_model, get_model_by_litellm
    row = get_model(model) or get_model_by_litellm(model)
    if row:
        return _caps_from_row(row)
    return dict(_DEFAULT_CAPABILITIES)


def get_model_context_window(model: str) -> int:
    """Return the effective context window for ``model``."""
    from llm.context_window import get_context_window
    return get_context_window(model)


# Cap the provider-published output limit CORSIA reserves room for.
_DEFAULT_MAX_OUTPUT_TOKENS_CAP = 32000


def get_model_max_output_tokens(model: str, fallback: int) -> int:
    """Return the provider-published output limit for a model, capped.

    Output limits are the provider's settings, so no CORSIA-stored value
    (such as a ``model_profiles.max_tokens`` row) changes the result.
    """
    from llm.catalog import get_model, get_model_by_litellm
    row = get_model(model) or get_model_by_litellm(model)
    if row:
        catalog_max = row.get("default_max_tokens")
        if catalog_max:
            return min(int(catalog_max), _DEFAULT_MAX_OUTPUT_TOKENS_CAP)

    return fallback


def snapshot_active_threads(exclude_thread: str | None = None) -> list[dict]:
    """Copy active in-memory thread facts on the event-loop thread."""
    cutoff = utc_now() - timedelta(minutes=15)
    return [
        {"thread_id": thread_id, **info}
        for thread_id, info in state._active_thread_models.items()
        if thread_id != exclude_thread
        and (info.get("ws_connected") or info["last_active"] > cutoff)
    ]


def get_active_local_threads(active_threads: list[dict]) -> list[dict]:
    """Return active threads whose catalog target is managed oMLX.

    Only the managed engine runs on this host, so only ``omlx@`` dispatch forms
    are local-contention relevant. External providers (``external@``) are
    user-run elsewhere and excluded (precise local/cloud classification arrives
    with cost_intent in PR2)."""
    from llm.catalog import get_model

    active = []
    for info in active_threads:
        model = get_model(info.get("model_id", ""))
        if model and str(model.get("litellm_model", "")).startswith("omlx@"):
            active.append(info)
    return active


def provider_from_model(model: str) -> str:
    """Return the canonical provider key for a model string."""
    if not model:
        return "unknown"
    if model.startswith("omlx@"):
        return "omlx"
    if model.startswith("external@"):
        return "external"
    return model.split("/")[0] if "/" in model else "unknown"


_CLOUD_PROVIDERS = {
    "anthropic/": {"config_key": "anthropic"},
    "openai/": {"config_key": "openai"},
}


def native_transport_identity(model: str) -> tuple[str | None, str | None]:
    """Return Native wire provider/model identity without endpoint resolution."""
    from llm.local_backends import find_backend

    backend = find_backend(model)
    if backend is not None:
        return backend.transport_identity(model)
    for prefix in _CLOUD_PROVIDERS:
        if model.startswith(prefix):
            effective_model = model[len(prefix):]
            return prefix[:-1], effective_model or None
    return None, None


def _resolve_api_key(config_key: str) -> str:
    """Resolve a cloud provider's key from the key store. The environment is not read."""
    from infra.key_store import resolve_provider

    return resolve_provider(config_key) or ""


def make_provider_params(model: str) -> dict:
    """Create provider params dict for the given model.

    Local backends use the local-backend registry; cloud providers resolve keys.
    """
    from llm.local_backends import find_backend

    backend = find_backend(model)
    if backend is not None:
        return backend.make_provider_params(model)

    api_key = None
    api_base = None
    for prefix, cfg in _CLOUD_PROVIDERS.items():
        if model.startswith(prefix):
            api_key = _resolve_api_key(cfg["config_key"])
            break

    return {"model": model, "api_key": api_key, "api_base": api_base, "uses_streaming": True}
