"""Codex dynamic-tool request and response wire grammar."""

from __future__ import annotations

import json
import inspect
from typing import Any

from core.media_delivery import ToolMediaContent, codex_tool_content_items
from infra.tool_result_errors import is_error_result
from tools.recorded_call import (
    CompletedToolCall, CompletedToolFailure, completed_tool_failure,
)


def dynamic_tool_values(params: dict) -> tuple[str | None, dict | None, dict | None]:
    """Validate the existing aliases for dynamic tool names and arguments."""
    name = params.get("tool") or params.get("name") or params.get("toolName")
    arguments = params.get("arguments") or params.get("input")
    arguments = arguments or params.get("params") or {}
    if not name:
        return None, None, {"code": -32602,
                            "message": "Dynamic tool request missing tool name"}
    if not isinstance(arguments, dict):
        return None, None, {"code": -32602,
                            "message": "Dynamic tool arguments must be an object"}
    return str(name), arguments, None


def dynamic_tool_response(content: Any) -> dict:
    """Project one CORSIA result into Codex's DynamicToolCallResponse shape."""
    text = content if isinstance(content, str) else json.dumps(content)
    is_error = is_error_result(content)
    media = getattr(content, "media", None)
    items = (
        codex_tool_content_items(ToolMediaContent(text=text, image=media.image))
        if media is not None
        else codex_tool_content_items(None, text=text)
    )
    return {"contentItems": items, "success": not is_error}


async def fork_reply_fits(session, reply, usage=None) -> bool:
    """Await a fork's accounting handoff on its existing tool boundary."""
    boundary = session.fork_tool_result_fits
    if boundary is None:
        return True
    result = boundary(reply) if usage is None else boundary(reply, usage)
    return bool(await result if inspect.isawaitable(result) else result)


async def execute_dynamic_tool(session, name: str, arguments: dict, params: dict):
    """Execute one admitted callback and check fork replies before writing."""
    from subagents.harness_tool_bridge import execute_tool, settle_codex_tool_result

    boundary = session.fork_tool_result_fits if session.fork_id else None
    if boundary is not None and not await fork_reply_fits(session, None):
        session._request_terminate()
        return None, None
    provider_call_id = str(params.get("callId") or params.get("id") or "")
    assistant_step_id = (
        session.declared_tool_step_ids.get(provider_call_id)
        if session.fork_id is None else None
    )
    if session.fork_id is None and not assistant_step_id:
        from core._tool_result_delivery_support import ToolResultBindingError

        raise ToolResultBindingError(
            "Codex dynamic tool request lacks its exact Assistant Step identity"
        )
    fork_kwargs = (
        {"fork_id": session.fork_id} if session.fork_id is not None else {}
    )
    # Capability follows the selected catalog entry, not a transport alias.
    model_identity = session.catalog_model_id or session.model
    model_kwargs = {"model": model_identity} if model_identity else {}
    from core.fork_tools import ForkToolBoundary

    if isinstance(boundary, ForkToolBoundary):
        fork_kwargs["fork_policy"] = boundary
    content = await execute_tool(
        session.tool_registry, thread_id=session.corsia_thread_id,
        turn_identity=session.corsia_turn_identity,
        invocation_id=provider_call_id, provider_tool_call_id=provider_call_id,
        assistant_step_id=assistant_step_id,
        name=name, arguments=arguments, harness="codex", **model_kwargs, **fork_kwargs,
    )
    try:
        reply = dynamic_tool_response(content)
        if session.fork_id is None:
            await settle_codex_tool_result(session, content, reply)
        if boundary is not None and not await fork_reply_fits(session, reply):
            # The session writer sees _closing and cannot release even an error reply.
            session._request_terminate()
            return None, None
    except CompletedToolFailure:
        raise
    except Exception as error:
        completion = getattr(content, "completion", None) or CompletedToolCall(
            name=name, result=content, arguments=dict(arguments),
            thread_id=session.corsia_thread_id,
            turn_id=session.corsia_turn_identity.turn_id if session.corsia_turn_identity else None,
            invocation_id=provider_call_id, provider_tool_call_id=provider_call_id,
            assistant_step_id=assistant_step_id,
            fork_id=session.fork_id,
        )
        raise completed_tool_failure(
            (completion,), error, harness="codex",
        ) from error
    return reply, None
