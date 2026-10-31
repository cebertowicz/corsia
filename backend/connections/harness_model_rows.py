"""Normalize Claude Code and Codex model listings into catalog rows.

Pure functions: no I/O, no catalog access. ``harness_models`` owns discovery
and persistence.
"""

from __future__ import annotations

import re
from typing import Any

from connections.claude_unlisted import ALIAS_KEY

CACHE_KEY = "harness_model_cache"
CLAUDE_SOURCE = "claude-stream-json:list_models"
CODEX_SOURCE = "codex-app-server:model/list"
VERSION = re.compile(r"\d+\.\d+\.\d+")
_CLAUDE_IMAGE_INPUT_MIN_VERSION = (2, 1, 218)


def _row(
    raw: dict[str, Any], *, selector: str, resolved: str,
    capabilities: dict[str, Any], is_default: bool,
    harness_version: str, source: str, timestamp: str,
) -> dict[str, Any]:
    return {
        "selector_id": selector,
        "display_name": str(raw.get("displayName") or selector),
        "resolved_identity": resolved,
        "description": str(raw.get("description") or ""),
        "capabilities": capabilities,
        "is_default": is_default,
        "harness_version": harness_version,
        "source": source,
        "discovered_at": timestamp,
        "available": True,
        "last_error": None,
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _with_context(
    row: dict[str, Any], *, raw_tokens: int, max_tokens: int,
    effective_tokens: int, effective_percent: int, source: str,
    version: str, observed_at: str,
) -> dict[str, Any]:
    return {
        **row,
        "context_window": effective_tokens,
        "context_observation": {
            "source": source,
            "version": version,
            "observed_at": observed_at,
            "raw_tokens": raw_tokens,
            "max_tokens": max_tokens,
            "effective_tokens": effective_tokens,
            "effective_percent": effective_percent,
        },
    }


def _claude_typed_images_supported(harness_version: str) -> bool:
    match = VERSION.fullmatch(harness_version.strip())
    if match is None:
        return False
    version = tuple(int(part) for part in match.group().split("."))
    return version >= _CLAUDE_IMAGE_INPUT_MIN_VERSION


def normalize_claude_models(
    payload: dict[str, Any], *, harness_version: str, timestamp: str
) -> list[dict[str, Any]]:
    """Normalize Claude stream-JSON ``list_models`` rows."""
    raw_models = payload.get("models") or []
    default_resolved = next(
        (
            str(raw.get("resolvedModel") or "").strip()
            for raw in raw_models
            if str(raw.get("value") or "").strip() == "default"
            and str(raw.get("resolvedModel") or "").strip()
        ),
        None,
    )
    models = []
    for raw in raw_models:
        selector = str(raw.get("value") or "").strip()
        resolved = str(raw.get("resolvedModel") or "").strip()
        if not selector or not resolved or selector == "default":
            continue
        row = _row(
            raw, selector=selector, resolved=resolved,
            capabilities={
                **(
                    {"input_modalities": ["text", "image"]}
                    if _claude_typed_images_supported(harness_version)
                    else {}
                ),
                "supports_effort": bool(raw.get("supportsEffort")),
                "effort_levels": list(raw.get("supportedEffortLevels") or []),
                "adaptive_thinking": bool(raw.get("supportsAdaptiveThinking")),
                "fast_mode": bool(raw.get("supportsFastMode")),
                "auto_mode": bool(raw.get("supportsAutoMode")),
            },
            is_default=False, harness_version=harness_version,
            source=CLAUDE_SOURCE, timestamp=timestamp,
        )
        if raw.get(ALIAS_KEY):
            row["alias_of"] = str(raw[ALIAS_KEY])
        usage = raw.get("contextUsage")
        if isinstance(usage, dict):
            raw_tokens = _positive_int(usage.get("rawMaxTokens"))
            max_tokens = _positive_int(usage.get("maxTokens"))
            if raw_tokens is not None and max_tokens is not None:
                row = _with_context(
                    row, raw_tokens=raw_tokens, max_tokens=max_tokens,
                    effective_tokens=max_tokens,
                    effective_percent=round(max_tokens * 100 / raw_tokens),
                    source="claude-stream-json:get_context_usage",
                    version=harness_version, observed_at=timestamp,
                )
        models.append(row)

    if default_resolved is not None:
        matching = [
            row for row in models
            if row["resolved_identity"] == default_resolved
        ]
        if matching:
            next(
                (row for row in matching if "alias_of" not in row),
                matching[0],
            )["is_default"] = True
    return models


def normalize_codex_models(
    payload: dict[str, Any], *, harness_version: str, timestamp: str
) -> list[dict[str, Any]]:
    """Normalize Codex app-server ``model/list`` rows."""
    models = []
    for raw in payload.get("data") or []:
        selector = str(raw.get("id") or "").strip()
        resolved = str(raw.get("model") or "").strip()
        if not selector or not resolved or raw.get("hidden"):
            continue
        efforts = [item.get("reasoningEffort") for item in
                   raw.get("supportedReasoningEfforts") or []
                   if item.get("reasoningEffort")]
        row = _row(
            raw, selector=selector, resolved=resolved,
            capabilities={
                "input_modalities": list(raw.get("inputModalities") or []),
                "reasoning_efforts": efforts,
                "default_reasoning_effort": raw.get("defaultReasoningEffort"),
                "supports_personality": bool(raw.get("supportsPersonality")),
            },
            is_default=bool(raw.get("isDefault")),
            harness_version=harness_version, source=CODEX_SOURCE,
            timestamp=timestamp,
        )
        context = raw.get("contextMetadata")
        if isinstance(context, dict):
            raw_tokens = _positive_int(context.get("context_window"))
            max_tokens = _positive_int(context.get("max_context_window"))
            percent = _positive_int(context.get("effective_context_window_percent"))
            if raw_tokens is not None:
                max_tokens = max_tokens or raw_tokens
                percent = min(percent or 100, 100)
                effective = raw_tokens * percent // 100
                row = _with_context(
                    row, raw_tokens=raw_tokens, max_tokens=max_tokens,
                    effective_tokens=effective, effective_percent=percent,
                    source="codex-debug-models", version=harness_version,
                    observed_at=timestamp,
                )
        models.append(row)
    return models


def retain_discovered_context(row: dict, previous: dict | None) -> dict:
    """Keep exact-identity context evidence when this refresh could not re-observe it."""
    result = dict(row)
    if (result.get('context_window') is None and previous
            and result.get('resolved_identity')
            and result.get('resolved_identity') == previous.get('resolved_identity')):
        result['context_window'] = previous.get('context_window')
        if isinstance(previous.get('context_observation'), dict):
            result['context_observation'] = previous['context_observation']
    return result
