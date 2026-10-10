"""oMLX server-setting descriptors for the model settings editor.

Sampling is the provider's setting, never a request parameter: CORSIA sends
no temperature or other sampling knob on any call. The one editor that exists
reads and writes oMLX's own per-model settings through its admin API, so the
values live on the oMLX server; an empty field means oMLX's own default.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_OMLX_FIELDS: list[dict[str, Any]] = [
    {"name": "temperature", "label": "Temperature", "type": "float",
     "min": 0, "max": 2, "step": 0.05},
    {"name": "top_p", "label": "top_p", "type": "float",
     "min": 0, "max": 1, "step": 0.01},
    {"name": "top_k", "label": "top_k", "type": "int",
     "min": 0, "max": 1000, "step": 1},
    {"name": "repetition_penalty", "label": "Repetition penalty",
     "type": "float", "min": 0, "max": 2, "step": 0.05},
    {"name": "repetition_context_size", "label": "Repetition window",
     "type": "int", "min": 0, "max": 8192, "step": 1},
    {"name": "frequency_penalty", "label": "Frequency penalty",
     "type": "float", "min": -2, "max": 2, "step": 0.05},
    {"name": "presence_penalty", "label": "Presence penalty",
     "type": "float", "min": -2, "max": 2, "step": 0.05},
    {"name": "max_tokens", "label": "Max output tokens", "type": "int",
     "min": 1, "max": 1_000_000, "step": 64},
]

_OMLX_SILENTLY_DROPPED = frozenset({"repetition_context_size"})


def inference_params_for(
    provider: str,
    model_id: str,
    *,
    server_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return editor field descriptors; only oMLX rows have server settings."""
    if provider != "omlx":
        return []
    silently_dropped = _resolve_silently_dropped(server_id)

    out: list[dict[str, Any]] = []
    for field in _OMLX_FIELDS:
        entry = dict(field)
        if field["name"] in silently_dropped:
            entry["supported"] = False
            entry["unsupported_reason"] = (
                "oMLX silently drops this setting, so it would have no "
                "effect. Track it upstream."
            )
        else:
            entry["supported"] = True
        out.append(entry)
    return out


def _resolve_silently_dropped(server_id: str | None) -> set[str]:
    """Pick the unsupported setting names for this oMLX server."""
    if server_id:
        api_base = _api_base_for_server(server_id)
        if api_base:
            from llm.providers.omlx_introspection import silently_dropped_cached
            return set(silently_dropped_cached(api_base))
    return set(_OMLX_SILENTLY_DROPPED)


def _api_base_for_server(server_id: str) -> str | None:
    """The server's OpenAI-compatible base URL, from its one owner.

    Delegated, not re-derived: a cloud endpoint's base carries a scheme and a
    path that host+port cannot reproduce, so composing one here pointed
    parameter resolution at a URL that was never the endpoint.
    """
    from llm.network_endpoints import build_api_base

    try:
        return build_api_base(server_id)
    except (KeyError, TypeError, ValueError, sqlite3.Error) as e:
        from loguru import logger
        logger.warning("endpoint resolution failed server={} err={}", server_id, e)
        return None
