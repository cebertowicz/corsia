"""Model metadata normalization helpers."""

from __future__ import annotations

from typing import Any


_CONTEXT_WINDOW_KEYS = (
    "max_context_window",
    "context_length",
    "context_window",
    "max_model_len",
    "max_model_length",
    "model_max_length",
    "max_position_embeddings",
    "max_sequence_length",
    "max_seq_len",
    "seq_length",
    "n_ctx",
)

_CONTEXT_WINDOW_CONTAINERS = (
    "config",
    "generation_config",
    "generation_defaults",
    "model_config",
    "settings",
)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else None
    return None


def extract_context_length(payload: Any) -> int | None:
    """Derive model context length from provider/HF metadata without guessing."""
    if not isinstance(payload, dict):
        return None

    for key in _CONTEXT_WINDOW_KEYS:
        parsed = _positive_int(payload.get(key))
        if parsed is not None:
            return parsed

    for key in _CONTEXT_WINDOW_CONTAINERS:
        parsed = extract_context_length(payload.get(key))
        if parsed is not None:
            return parsed

    return None
