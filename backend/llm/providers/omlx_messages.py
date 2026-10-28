"""oMLX-specific outgoing message shaping."""

from __future__ import annotations

import json
from typing import Any


def shape_messages(
    messages: list[dict[str, Any]], model: str, server_id: str | None, *, is_omlx: bool = True,
) -> list[dict[str, Any]]:
    """Work around Gemma 4's tool-response template until oMLX fixes it."""
    if not is_omlx or not server_id or not model.startswith("openai/"):
        return messages
    from llm.catalog import get_model_by_litellm

    selector = f"omlx@{server_id}/{model.removeprefix('openai/')}"
    row = get_model_by_litellm(selector)
    if not row or row.get("litellm_model") != selector:
        return messages
    metadata = json.loads(row.get("metadata_json") or "{}")
    if metadata.get("config_model_type") not in ("gemma4", "gemma4_unified"):
        return messages
    # oMLX 0.7.1rc1 merges tool responses into the preceding assistant message;
    # the Gemma 4 template closes the turn when that message has content.
    # Remove this workaround once oMLX handles it.
    return [
        {**msg, "content": None}
        if msg.get("role") == "assistant" and msg.get("tool_calls") and isinstance(msg.get("content"), str)
        else msg
        for msg in messages
    ]
