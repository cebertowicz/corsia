"""Render and fit provider input shared by disposable forks."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from typing import Any

from core.fork_capture import warm_capture_refused
from core import fork_cold_count
from core.fork_types import FORK_OUTPUT_TOKENS

COLD_FIT_FRAMING_MARGIN_DIVISOR = 20


def fork_binary(target, db) -> str | None:
    """Resolve the configured binary for one selected fork harness."""
    if target is None or target.harness == "native":
        return None
    if target.harness == "claude-code":
        from connections.models import get_connection

        configured = get_connection("claude-code", db=db) or {}
        value = os.environ.get("CLAUDE_BIN") or (configured.get("config") or {}).get(
            "binary"
        )
        return str(value or "claude")
    if target.harness == "codex":
        from subagents.codex_runtime_config import codex_binary, codex_connection

        return str(codex_binary(codex_connection(db)))
    raise ValueError(f"unsupported fork harness: {target.harness}")


async def capture_warm_fork_source(
    thread_id: str,
    boundary,
    *,
    target,
    binary: str | None,
    reasoning_effort: str | None,
    live: Any | None = None,
    prefix_bytes: int | None = None,
):
    """Capture one warm prefix through the selected fork harness adapter."""
    del reasoning_effort
    if target is None:
        return None
    harness = target.harness
    if harness not in {"claude-code", "codex"}:
        return warm_capture_refused(thread_id, harness, "native_source_unsupported")
    if binary is None:
        return warm_capture_refused(thread_id, harness, "no_binary")
    if harness == "claude-code":
        from core.fork_cc_boundary import capture_registered_boundary_warm_source

        warm_boundary = await asyncio.to_thread(
            _durable_warm_boundary, thread_id, boundary, harness,
        )
        if warm_boundary is None:
            return warm_capture_refused(thread_id, harness, "no_warm_boundary")
        return await capture_registered_boundary_warm_source(
            thread_id, boundary, warm_boundary, target=target, binary=binary, live=live,
        )
    from core.fork_codex import capture_warm_source, warm_tool_boundary_prefix_bytes

    if prefix_bytes is None:
        warm_boundary = await asyncio.to_thread(
            _durable_warm_boundary, thread_id, boundary, harness,
        )
        if warm_boundary is None:
            return warm_capture_refused(thread_id, harness, "no_warm_boundary")
        prefix_bytes = await asyncio.to_thread(
            warm_tool_boundary_prefix_bytes, thread_id, warm_boundary,
        )
        if isinstance(prefix_bytes, str):
            return warm_capture_refused(thread_id, harness, prefix_bytes)
    return await capture_warm_source(
        thread_id, boundary, target=target, binary=binary, prefix_bytes=prefix_bytes,
    )


def _durable_warm_boundary(thread_id: str, boundary, harness: str):
    """Project the canonical history boundary paired with a provider prefix."""
    from history.projection import render_portable_history
    from infra.database import get_db
    from core.fork_capture import warm_boundary

    db = get_db()
    try:
        projection = render_portable_history(
            thread_id, db=db, include_unfinished_turn=True,
        )
    finally:
        db.close()
    if harness not in {"claude-code", "codex"}:
        return None
    return warm_boundary(projection, boundary)


def codex_cold_surface(
    working_dir: str, *, writer: object, registry=None, definitions=None, developer=None,
):
    """Freeze and count the known Codex tool surface for one cold fork."""
    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions
    from subagents.managed_launch import (
        codex_developer_instructions_with_write_path,
        codex_tools_are_write_capable,
    )

    registry = registry if registry is not None else build_tool_registry(working_dir)
    if definitions is None:
        definitions = dynamic_tool_definitions(registry)
    dynamic_tool_specs = tuple(deepcopy(to_dynamic_tool_specs(definitions)))
    names = {str(definition.get("name") or "") for definition in dynamic_tool_specs}
    developer = codex_developer_instructions_with_write_path(
        developer, write_capable=codex_tools_are_write_capable(names), read_only=True,
    )
    payload: dict[str, object] = {"dynamicTools": dynamic_tool_specs}
    if developer is not None:
        payload["developerInstructions"] = developer
    return fork_cold_count.cold_input_tokens(payload, writer), registry, dynamic_tool_specs, developer


def _cold_input_allowance(
    window: int | None, output_tokens: int, round_trip_tokens: int = 0,
) -> int:
    from core.fork_fit import ColdFoldInputTooLarge

    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ColdFoldInputTooLarge("cold fork has no proved context window")
    if (
        isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens <= 0
    ):
        raise ValueError("fork output allowance must be a positive integer")
    allowance = window - output_tokens - round_trip_tokens - window // COLD_FIT_FRAMING_MARGIN_DIVISOR
    if allowance <= 0:
        raise ColdFoldInputTooLarge("fork output and framing leave no input room")
    return allowance


def require_cold_fork_room(
    window: int | None, instruction: str, *, writer: object, protected=(),
    output_tokens: int = FORK_OUTPUT_TOKENS,
) -> None:
    """Refuse a known-impossible fork input before loading a provider surface."""
    from core.fork_fit import ColdFoldInputTooLarge

    minimum = fork_cold_count.cold_input_tokens(
        ({"role": "system", "content": instruction}, *protected), writer,
    )
    if minimum > _cold_input_allowance(window, output_tokens):
        raise ColdFoldInputTooLarge("protected fork input leaves no writer room")


def fit_cold_fork_input(request, *, provider_surface_tokens: int, round_trip_tokens: int = 0):
    """Fit a frozen source exactly once using the loaded provider surface."""
    from core.fork_fit import render_cold_fold_input

    source = request.cold_source
    if source is None or request.warm_source is not None:
        raise TypeError("cold fit requires one cold source and no warm source")
    if (
        isinstance(provider_surface_tokens, bool)
        or not isinstance(provider_surface_tokens, int)
        or provider_surface_tokens < 0
    ):
        raise ValueError("cold writer surface must be a non-negative integer")

    def count(messages) -> int:
        return provider_surface_tokens + fork_cold_count.cold_input_tokens(
            ({"role": "system", "content": request.instruction}, *messages), request.target,
        )

    return render_cold_fold_input(
        source.projection,
        max_input_tokens=_cold_input_allowance(
            request.target.context_window, request.max_output_tokens, round_trip_tokens,
        ),
        protected_blocks=source.protected_blocks,
        count_tokens=count,
    )
