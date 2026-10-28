"""Claude Code loopback tool-bridge routes.

GET  /api/cc/_tool-bridge/{session_id} → the live handle's tool definitions
POST /api/cc/_tool-bridge/{session_id} → execute one tool under that handle

Loopback-only and authenticated by the live handle's rotating tool token; the
admission gates live here so an in-flight call can tell a settling handle
transition apart from a genuinely bad token.
"""

from __future__ import annotations

import asyncio
from secrets import compare_digest

import anyio
from fastapi import APIRouter, Header, HTTPException, Request
from starlette.responses import StreamingResponse

from infra import state
from infra.db_async import run_in_db
from infra.local_connection import is_direct_loopback
from subagents.cc_runtime_state import (
    _LIVE,
    _Live,
    _SPAWN_LOCK_TIMEOUT_S,
    TOOL_TOKEN_ROTATED_DETAIL,
    is_grace_tool_token,
    session_recently_rotated,
)
from subagents.harness_tool_bridge import execute_tool

router = APIRouter(prefix="/api/cc", tags=["cc"])


class _DeferredStartStreamingResponse(StreamingResponse):
    """Keep lazy cancellation while allowing pre-body HTTP errors to render."""

    async def stream_response(self, send) -> None:
        """Resolve the first body chunk before committing the HTTP status."""
        iterator = aiter(self.body_iterator)
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            first = None
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.raw_headers,
        })
        if first is not None:
            if not isinstance(first, bytes | memoryview):
                first = first.encode(self.charset)
            await send({
                "type": "http.response.body",
                "body": first,
                "more_body": True,
            })
        async for chunk in iterator:
            if not isinstance(chunk, bytes | memoryview):
                chunk = chunk.encode(self.charset)
            await send({
                "type": "http.response.body",
                "body": chunk,
                "more_body": True,
            })
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _tool_live(
    request: Request,
    thread_id: str,
    token: str,
    *,
    expected_live=None,
):
    """Authenticate one loopback tool-bridge call against the current handle.

    Live-handle transitions rotate the tool token, so a call issued under the
    immediately-previous token stays valid for a bounded dual-accept window
    and executes under the current handle's authority and admission gates.
    Every rejection names its exact condition — rotation, unknown session, or
    a session without a live handle — because an in-flight call must be able
    to distinguish a settling transition from a genuinely bad token.
    """
    if not is_direct_loopback(request):
        raise HTTPException(403, "Claude Code tool bridge is loopback-only")
    live = _LIVE.get(thread_id)
    expected = getattr(live, "tool_token", "")
    if not expected or not compare_digest(token, expected):
        if is_grace_tool_token(thread_id, token):
            if getattr(live, "tool_registry", None) is None:
                # A predecessor token with no usable successor yet
                # (mid-transition or post-close). The exact detail lets the
                # caller retry once instead of treating its own runtime's
                # reshuffling as an auth failure.
                raise HTTPException(401, TOOL_TOKEN_ROTATED_DETAIL)
            # Dual-accept: the immediately-previous token authenticates
            # against the settled successor; execution still faces the same
            # admission gates as a current-token call.
        elif live is not None:
            raise HTTPException(401, "Invalid Claude Code tool token")
        elif session_recently_rotated(thread_id):
            raise HTTPException(401, "Claude Code session has no live handle")
        else:
            raise HTTPException(401, "Unknown Claude Code session")
    if expected_live is not None and live is not expected_live:
        # The handle rotated between admission and execution start; refuse to
        # execute under the retired authority and name the condition.
        raise HTTPException(401, TOOL_TOKEN_ROTATED_DETAIL)
    if getattr(live, "tool_registry", None) is None:
        raise HTTPException(503, "Claude Code tool registry is unavailable")
    return live


def _require_tool_execution_admitted(live: _Live) -> None:
    if (
        not getattr(live, "turn_active", False)
        or getattr(live, "interrupt_requested", False)
    ):
        raise HTTPException(
            409,
            "Claude Code tools are unavailable outside an active provider turn",
        )


async def _assistant_step_for_call(live: _Live, provider_call_id: str) -> str:
    """Wait for the durable CC Step that owns one exact loopback call."""
    from core._tool_result_delivery_support import ToolResultBindingError

    if not provider_call_id:
        raise ToolResultBindingError(
            "CC tool invocation lacks its exact provider Assistant Step"
        )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SPAWN_LOCK_TIMEOUT_S
    while True:
        step_id = live.tool_step_ids.get(provider_call_id)
        if step_id is not None:
            return step_id
        live.tool_steps_ready.clear()
        # Close the map-update/event-clear race before blocking.
        step_id = live.tool_step_ids.get(provider_call_id)
        if step_id is not None:
            return step_id
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(live.tool_steps_ready.wait(), timeout=remaining)
        except TimeoutError:
            break
    raise ToolResultBindingError(
        "CC tool invocation lacks its exact provider Assistant Step"
    )


async def _claim_declared_tool_call(
    live: _Live, thread_id: str, body: dict,
) -> tuple[str, str]:
    """Bind the exact provider name/args and claim its call before any effect."""
    from core._tool_result_delivery_support import (
        ToolResultBindingError,
        select_provider_call,
    )
    from infra.database import get_db

    identity = live.corsia_turn
    if identity is None:
        raise ToolResultBindingError("CC tool invocation lacks an active Turn")
    call_id = str(body.get("provider_tool_call_id") or "")
    step_id = await _assistant_step_for_call(live, call_id)

    def _validate() -> None:
        db = get_db()
        try:
            select_provider_call(
                db=db,
                thread_id=thread_id,
                assistant_step_id=step_id,
                provider_tool_call_id=call_id,
                name=str(body.get("name") or ""),
                arguments=body.get("arguments") or {},
            )
        finally:
            db.close()

    await run_in_db(_validate)
    if (
        live.corsia_turn != identity
        or live.tool_step_ids.get(call_id) != step_id
        or call_id in live.claimed_tool_calls
    ):
        raise ToolResultBindingError("CC provider tool call is stale or already claimed")
    live.claimed_tool_calls.add(call_id)
    return call_id, step_id


async def _settle_held_result(live: _Live, thread_id: str, result, identity) -> bool:
    """Settle a durable tool result; return whether a checkpoint resumed."""
    from subagents.harness_tool_bridge import settle_cc_tool_result

    # The result is durable, so settling it belongs to the Turn. A
    # tool-boundary checkpoint may retire this caller's process, and the
    # disconnect that follows must not cancel that swap part-way (#1867).
    # The exact live's own cancellation (Stop, process exit) still applies.
    with anyio.CancelScope(shield=True):
        resumed = await settle_cc_tool_result(live, thread_id, result, identity)
    return resumed


async def _execute_admitted_tool(
    live: _Live,
    thread_id: str,
    body: dict,
) -> str:
    """Execute one tool while the exact CC live owns its backend task."""
    model = getattr(live, "catalog_model_id", None) or getattr(live, "model", None)
    model_kwargs = {"model": model} if model else {}
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("CC tool execution has no owning asyncio task")
    live.admitted_tool_tasks.add(task)
    turn_identity = getattr(live, "corsia_turn", None)
    failure_fenced = False
    try:
        identity_kwargs = {}
        if turn_identity is not None:
            identity_kwargs["turn_identity"] = turn_identity
        # Only an actual target identity is forwarded; an unresolved model must
        # leave the call shape exactly as it was.
        identity_kwargs.update(model_kwargs)
        provider_tool_call_id, assistant_step_id = await _claim_declared_tool_call(
            live, thread_id, body,
        )
        live.tool_execution_admitted = True
        result = await execute_tool(
            live.tool_registry,
            thread_id=thread_id,
            invocation_id=str(body.get("invocation_id") or ""),
            provider_tool_call_id=provider_tool_call_id,
            assistant_step_id=assistant_step_id,
            name=str(body.get("name") or ""),
            arguments=body.get("arguments") or {},
            harness="claude-code",
            **identity_kwargs,
        )
        resumed = await _settle_held_result(
            live, thread_id, result, result.turn_identity or turn_identity,
        )
    except Exception as error:
        from subagents.cc_runtime_quiescence import fence_tool_failure

        failure_fenced = fence_tool_failure(thread_id, live, turn_identity, error)
        raise
    else:
        # Still admitted: the CLI reads this steer before its next model call
        # (#2204). A steer failure never fails the completed tool.
        if not resumed:
            from subagents.cc_runtime_steering import steer_held_reply

            await steer_held_reply(thread_id)
        return result
    finally:
        live.admitted_tool_tasks.discard(task)
        if failure_fenced:
            from subagents.cc_runtime_quiescence import finish_tool_failure
            from subagents.cc_stream_control import retain_terminal_driver

            retain_terminal_driver(live.session, finish_tool_failure(thread_id, live, turn_identity))


async def _tool_result_stream(
    request: Request,
    live: _Live,
    thread_id: str,
    body: dict,
    token: str,
):
    """Yield one authenticated Claude tool result after final admission checks."""
    admitted_live = _tool_live(
        request,
        thread_id,
        token,
        expected_live=live,
    )
    _require_tool_execution_admitted(admitted_live)
    executed = await _execute_admitted_tool(admitted_live, thread_id, body)
    media = getattr(executed, "media", None)
    if media is None:
        yield str(executed)
        return

    from core.media_delivery import ToolMediaContent, cc_tool_bridge_payload

    yield cc_tool_bridge_payload(
        ToolMediaContent(text=str(executed), image=media.image),
        secret=token,
    )


@router.get("/_tool-bridge/{thread_id}", include_in_schema=False)
async def list_cc_tools(
    request: Request,
    thread_id: str,
    token: str = Header("", alias="X-CORSIA-Tool-Token"),
):
    """List the exact tool definitions frozen onto one authenticated live handle."""
    if request.headers.get("X-CORSIA-Fork-Id"):
        from core.fork_cc_bridge import authenticate, definitions
        from infra.db_async import run_in_db
        from tools.connected_app_visibility import account_capabilities_for_thread

        handle = authenticate(request, thread_id, token)
        capabilities = await run_in_db(account_capabilities_for_thread, thread_id)
        return {"tools": list(definitions(handle, capabilities))}
    live = _tool_live(request, thread_id, token)
    definitions = getattr(live, "tool_definitions", None)
    if definitions is None:
        raise HTTPException(503, "Claude Code tool definitions are unavailable")
    return {"tools": list(definitions)}


@router.post("/_tool-bridge/{thread_id}", include_in_schema=False)
async def execute_cc_tool(
    request: Request,
    thread_id: str,
    body: dict,
    token: str = Header("", alias="X-CORSIA-Tool-Token"),
):
    """Execute one authenticated Claude Code tool call under its live handle."""
    if request.headers.get("X-CORSIA-Fork-Id"):
        from core.fork_cc_bridge import authenticate, result_stream

        authenticate(request, thread_id, token)
        return _DeferredStartStreamingResponse(
            result_stream(request, thread_id, token, body), media_type="text/plain",
        )
    live = _tool_live(request, thread_id, token)
    _require_tool_execution_admitted(live)

    stream = _tool_result_stream(request, live, thread_id, body, token)
    return _DeferredStartStreamingResponse(stream, media_type="text/plain")
