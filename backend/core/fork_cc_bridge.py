"""Authenticated per-fork CC bridge; never borrows a foreground live handle."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import secrets

from core.fork_runtime import ForkHandle, handles, running_fork
from core.fork_tools import ForkToolBoundary, allowed_definitions


@contextmanager
def bind_bridge(request, kind, cancel_event):
    from subagents.harness_tool_bridge import build_tool_registry, write_cc_mcp_config

    handle = running_fork(request.thread_id, request.checkpoint_id)
    owns_handle = handle is None
    if owns_handle:
        handle = ForkHandle(asyncio.current_task(), cancel_event, request.thread_id)
        handles[request.checkpoint_id] = handle
    config = None
    try:
        handle.kind, handle.request = kind, request
        handle.tool_registry = request.tool_registry or build_tool_registry(request.working_dir)
        handle.tool_token = secrets.token_urlsafe(32)
        config = write_cc_mcp_config(
            thread_id=request.thread_id,
            tool_token=handle.tool_token,
            fork_id=request.checkpoint_id,
        )
        configured = request.mcp_config is not None or request.tool_registry is not None
        yield replace(request, mcp_config=config) if configured else request, handle
    finally:
        handle.tool_token = ""
        handle.tool_executor = None
        if config:
            Path(config).unlink(missing_ok=True)
        if owns_handle and handles.get(request.checkpoint_id) is handle:
            handles.pop(request.checkpoint_id, None)


def authenticate(request, thread_id, token):
    from secrets import compare_digest
    from fastapi import HTTPException
    from infra.local_connection import is_direct_loopback

    if not is_direct_loopback(request):
        raise HTTPException(403, "Claude Code tool bridge is loopback-only")
    fork_id = request.headers.get("X-CORSIA-Fork-Id", "")
    handle = running_fork(thread_id, fork_id)
    if handle is None:
        raise HTTPException(409, "Fork execution is no longer running")
    if not handle.tool_token or not compare_digest(token, handle.tool_token):
        raise HTTPException(401, "Invalid fork tool token")
    return handle


async def result_stream(request, thread_id, token, body):
    handle = authenticate(request, thread_id, token)
    result = await execute(handle, body)
    media = getattr(result, "media", None)
    if media is None:
        yield str(result)
    else:
        from core.media_delivery import ToolMediaContent, cc_tool_bridge_payload

        yield cc_tool_bridge_payload(ToolMediaContent(text=str(result), image=media.image), secret=token)


def definitions(handle):
    """Advertise the source's list on a warm resume, the narrowed list on cold.

    A warm fork resumes the source's cached prefix, which embeds the source's
    tool list; changing it busts that cache. The kind's allowlist is still
    enforced at execution (``ForkToolBoundary``).
    """
    from subagents.harness_tool_bridge import dynamic_tool_definitions
    from tools.catalog import fork_tool_definitions

    source = handle.request.warm_source
    if source is not None:
        frozen = getattr(source, "tool_definitions", None)
        return frozen if frozen is not None else dynamic_tool_definitions(handle.tool_registry)
    return allowed_definitions(
        fork_tool_definitions(handle.tool_registry, handle.kind.tool_allowlist), handle.kind,
    )


async def execute(handle, body):
    from core.checkpoint_types import CheckpointNotAdmitted

    if handle.tool_executor is None:
        raise CheckpointNotAdmitted("fork tool boundary is not ready")
    name, arguments = str(body.get("name") or ""), body.get("arguments") or {}

    async def operation():
        from subagents.harness_tool_bridge import execute_tool

        return await execute_tool(
            handle.tool_registry, thread_id=handle.thread_id,
            checkpoint_id=handle.request.checkpoint_id, name=name, arguments=arguments,
            fork_policy=ForkToolBoundary(
                handle.kind, handle.request, handle.tool_registry, handle.cancel_event, None,
            ), harness="claude-code", model=handle.request.target.catalog_id,
        )

    return await handle.tool_executor(
        operation, str(body.get("provider_tool_call_id") or ""), name, arguments,
    )
