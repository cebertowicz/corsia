"""The exact live Native prefix a warm fork reuses, frozen with its digest."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from history.types import HistoryBoundary


@dataclass(frozen=True, slots=True)
class NativeWarmSource:
    """The exact message/tool prefix an ordinary Native send would use."""

    thread_id: str
    boundary: HistoryBoundary
    prefix_messages: tuple[dict[str, Any], ...]
    tool_definitions: tuple[dict[str, Any], ...]
    prefix_sha256: str
    provider_input_tokens: int
    rendered_input_tokens: int


def capture_live_warm_source(
    thread_id: str,
    boundary: HistoryBoundary,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    reasoning_effort: str | None,
    preserve_thinking: bool | None,
    provider_input_tokens: int,
    rendered_input_tokens: int,
) -> NativeWarmSource:
    """Freeze the exact live Native prefix already accepted by the provider."""
    frozen_messages = tuple(deepcopy(message) for message in messages)
    frozen_tools = tuple(deepcopy(tool) for tool in tools)
    if (
        isinstance(provider_input_tokens, bool)
        or not isinstance(provider_input_tokens, int)
        or provider_input_tokens < 0
        or isinstance(rendered_input_tokens, bool)
        or not isinstance(rendered_input_tokens, int)
        or rendered_input_tokens < 0
    ):
        raise ValueError("Native warm source requires a nonnegative occupancy anchor")
    return NativeWarmSource(
        thread_id=thread_id,
        boundary=boundary,
        prefix_messages=frozen_messages,
        tool_definitions=frozen_tools,
        prefix_sha256=_prefix_sha256(
            frozen_messages,
            frozen_tools,
            reasoning_effort,
            preserve_thinking,
        ),
        provider_input_tokens=provider_input_tokens,
        rendered_input_tokens=rendered_input_tokens,
    )


def _prefix_sha256(
    messages: tuple[dict[str, Any], ...],
    tools: tuple[dict[str, Any], ...],
    reasoning_effort: str | None,
    preserve_thinking: bool | None,
) -> str:
    payload = json.dumps(
        {
            "messages": messages,
            "tools": tools,
            "reasoning_effort": reasoning_effort or "",
            "preserve_thinking": preserve_thinking,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
