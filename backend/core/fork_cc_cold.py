"""One pre-generation context exchange on the disposable Claude fork pipe."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Any

from loguru import logger

from core.fork_types import (
    ProviderForkRejected,
    ProviderForkRequest,
)
from core.fork_types import (
    FORK_TOOL_ROUND_TRIP_TOKENS,
    ColdInputCallback,
)

_CONTEXT_ADMISSION_TIMEOUT_S = 30.0


def _corsia_mcp_status(record: dict, *, key: str) -> str:
    """Return the CORSIA server status from one Claude status record."""
    servers = record.get(key)
    if not isinstance(servers, list):
        return "not reported"
    for server in servers:
        if isinstance(server, dict) and server.get("name") == "corsia":
            status = server.get("status")
            return status if isinstance(status, str) and status else "reported without status"
    return "not listed"


def _admission_detail(reason: str, spawned_at: float, status: str, *, source: str) -> str:
    """Attach CLI MCP status and spawn-relative timing to one rejection."""
    elapsed = time.monotonic() - spawned_at
    return (
        f"{reason} (corsia MCP {source}: {status}; "
        f"elapsed from process spawn: {elapsed:.3f}s)"
    )


async def _control_response(
    process: Any, request_id: str, spawned_at: float, diagnostics: dict[str, str],
) -> dict:
    while True:
        line = await process.stdout.readline()
        if not line:
            raise ProviderForkRejected(_admission_detail(
                "Claude exited before cold context admission",
                spawned_at,
                diagnostics["init_status"],
                source="init status",
            ))
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderForkRejected("Claude returned invalid cold context data") from error
        if not isinstance(record, dict):
            raise ProviderForkRejected("Claude returned invalid cold context data")
        if record.get("type") == "system" and record.get("subtype") == "init":
            diagnostics["init_status"] = _corsia_mcp_status(record, key="mcp_servers")
            continue
        if record.get("type") in {"assistant", "result"}:
            raise ProviderForkRejected("Claude generated before cold context admission")
        response = record.get("response")
        if (
            record.get("type") == "control_response"
            and isinstance(response, dict)
            and response.get("request_id") == request_id
        ):
            return response


def _has_loaded_mcp_tools(context: dict) -> bool:
    categories = context.get("categories")
    if not isinstance(categories, list):
        return False
    return any(
        isinstance(category, dict)
        and category.get("name") == "MCP tools"
        and category.get("kind") == "used"
        and type(category.get("tokens")) is int
        and category["tokens"] > 0
        for category in categories
    )


def _validated_context(context: dict, request: ProviderForkRequest) -> tuple[int, int]:
    expected = request.target.expected_reported_model or request.target.transport_model
    if context.get("model") != expected:
        raise ProviderForkRejected(
            "Claude cold context does not match the selected model"
        )
    tokens, window = context.get("totalTokens"), context.get("maxTokens")
    if (
        not isinstance(tokens, int) or isinstance(tokens, bool)
        or not isinstance(window, int) or isinstance(window, bool)
    ):
        raise ProviderForkRejected("Claude cold context has no valid token counts")
    if tokens <= 0 or window <= 0 or (tokens > window and request.warm_source is None):
        raise ProviderForkRejected("Claude cold context has invalid occupancy")
    target_window = request.target.context_window
    if (
        isinstance(target_window, bool)
        or not isinstance(target_window, int)
        or target_window <= 0
    ):
        raise ProviderForkRejected("Claude cold request has no proved operator cap")
    return tokens, min(window, target_window)


async def _request_control(
    process: Any,
    request_id: str,
    subtype: str,
    spawned_at: float,
    deadline_at: float,
    diagnostics: dict[str, str],
) -> dict:
    frame = {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": subtype},
    }
    process.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
    await process.stdin.drain()
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(
        _control_response(process, request_id, spawned_at, diagnostics), remaining,
    )


async def _mcp_status(
    process: Any, spawned_at: float, deadline_at: float, diagnostics: dict[str, str],
) -> str:
    """Read dynamic MCP status after a configured server is absent from context."""
    try:
        response = await _request_control(
            process,
            "corsia-fork-mcp-status",
            "mcp_status",
            spawned_at,
            deadline_at,
            diagnostics,
        )
    except (ProviderForkRejected, TimeoutError):
        return "unavailable"
    payload = response.get("response")
    if response.get("subtype") != "success" or not isinstance(payload, dict):
        return "unavailable"
    return _corsia_mcp_status(payload, key="mcpServers")


async def _loaded_context(
    process: Any, request: ProviderForkRequest, spawned_at: float,
) -> tuple[int, int]:
    """Read context under the established write-relative admission deadline."""
    diagnostics = {"init_status": "not received"}
    deadline_at = time.monotonic() + _CONTEXT_ADMISSION_TIMEOUT_S
    try:
        response = await _request_control(
            process,
            "corsia-fork-context",
            "get_context_usage",
            spawned_at,
            deadline_at,
            diagnostics,
        )
    except TimeoutError as error:
        raise ProviderForkRejected(_admission_detail(
            "Claude cold context admission timed out",
            spawned_at,
            diagnostics["init_status"],
            source="init status",
        )) from error
    context = response.get("response")
    if response.get("subtype") != "success" or not isinstance(context, dict):
        raise ProviderForkRejected("Claude could not report its loaded cold context")
    if request.mcp_config and not _has_loaded_mcp_tools(context):
        status = await _mcp_status(process, spawned_at, deadline_at, diagnostics)
        raise ProviderForkRejected(_admission_detail(
            "Claude did not load the configured fork MCP tools",
            spawned_at,
            status,
            source="status",
        ))
    return _validated_context(context, request)


async def prepare_warm_payload(process, request, cancel_event, spawned_at: float) -> bytes | None:
    """Keep the warm prefix unchanged; None rejects first-input admission.

    The fork must retain its output allowance after reading the input.
    """
    from core.token_estimation import measure_request
    from subagents.cc_stream_frames import build_user_frame

    tokens, window = await _loaded_context(process, request, spawned_at)
    if cancel_event.is_set():
        raise asyncio.CancelledError
    reserve = request.max_output_tokens + FORK_TOOL_ROUND_TRIP_TOKENS

    def fits(instruction: str) -> bool:
        instruction_tokens = measure_request({"messages": [
            {"role": "user", "content": instruction},
        ]}, target=request.target).tokens
        return tokens + instruction_tokens + reserve <= window

    candidates = request.instruction_candidates or (request.instruction,)
    for index, instruction in enumerate(candidates):
        if fits(instruction):
            if index:
                logger.info(
                    "fork.instruction_skipped thread={} fork={} reason=no-room-at-fork",
                    request.thread_id, request.fork_id,
                )
            return build_user_frame(instruction)
    return None


async def prepare_cold_payload(
    process: Any,
    request: ProviderForkRequest,
    cancel_event: asyncio.Event,
    on_cold_input: ColdInputCallback | None,
    spawned_at: float,
) -> bytes:
    """Read this process's loaded surface, then fit once before any user frame."""
    from core.fork_render import fit_cold_fork_input
    from subagents.cc_stream_frames import build_user_frame

    tokens, window = await _loaded_context(process, request, spawned_at)
    bounded = replace(request, target=replace(request.target, context_window=window))
    fitted = await asyncio.to_thread(
        fit_cold_fork_input, bounded, provider_surface_tokens=tokens,
    )
    if on_cold_input is not None:
        await on_cold_input(fitted.first_included_seq)
    if cancel_event.is_set():
        raise asyncio.CancelledError
    return build_user_frame(fitted.as_prompt(request.instruction))
