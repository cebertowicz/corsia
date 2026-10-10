"""Shared readers for one durable message row metadata and declared tool calls."""

from __future__ import annotations

import json
from typing import Any


def parse_meta(raw: Any) -> dict:
    """Safely parse metadata JSON string or return a detached dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def assistant_calls(row: dict) -> list[dict]:
    meta = parse_meta(row.get("metadata"))
    calls = meta.get("llm_tool_calls") or meta.get("tool_calls") or []
    return [call for call in calls if isinstance(call, dict)]


def call_id(call: dict) -> str:
    value = call.get("id")
    return value if isinstance(value, str) else ""
