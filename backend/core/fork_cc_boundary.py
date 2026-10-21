"""Fail-closed Claude Code transcript capture at a settled tool boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.fork_cc_boundary_records import (
    boundary_prefix_cut,
    tool_prefix_from_stream,
)
from core.fork_capture import warm_capture_refused
from history.types import DurableBoundaryMessage as ClaudeTranscriptBoundary



def cc_tool_boundary_prefix_bytes(
    transcript: bytes,
    tool_call_id: str,
    boundary: ClaudeTranscriptBoundary,
    *,
    provider_session_id: str | None = None,
) -> int | None:
    """Return a prefix whose final semantic record is the durable through row."""
    if (
        not transcript
        or not isinstance(tool_call_id, str)
        or not tool_call_id
        or not isinstance(boundary, ClaudeTranscriptBoundary)
        or boundary.role not in {"assistant", "tool", "user"}
        or not isinstance(boundary.content, str)
        or (boundary.role == "tool" and not boundary.tool_call_id)
    ):
        return None
    from io import BytesIO

    prefix_bytes = tool_prefix_from_stream(
        BytesIO(transcript),
        tool_call_id,
        boundary,
        provider_session_id,
    )
    return None if isinstance(prefix_bytes, str) else prefix_bytes


def _prefix_bytes_from_path(
    transcript_path: str,
    tool_call_id: str,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None = None,
) -> int | str:
    """Stream and prove a bounded transcript prefix off the event loop.

    Returns the prefix length, or the one reason no prefix was proved.
    """
    path = Path(transcript_path)
    with path.open("rb") as stream:
        return tool_prefix_from_stream(
            stream,
            tool_call_id,
            boundary,
            provider_session_id,
        )


def _boundary_prefix_bytes_from_path(
    transcript_path: str,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str,
) -> int | str:
    """Stream one registered transcript and cut through the exact durable row.

    Returns the prefix length, or the one reason no prefix was proved.
    """
    path = Path(transcript_path)
    with path.open("rb") as stream:
        return boundary_prefix_cut(stream, boundary, provider_session_id)


async def capture_tool_boundary_warm_source(
    context: Any,
    live: Any,
    thread_id: str,
    current_tool_call_id: str | None,
):
    """Freeze a CC prefix only while its final tool result is withheld."""
    if context.target.harness != "claude-code":
        return warm_capture_refused(thread_id, "claude-code", "target_not_claude_code")
    if context.boundary is None:
        return warm_capture_refused(thread_id, "claude-code", "no_boundary")
    if not isinstance(
        getattr(context, "warm_boundary", None), ClaudeTranscriptBoundary,
    ):
        return warm_capture_refused(thread_id, "claude-code", "no_warm_boundary")
    if not isinstance(current_tool_call_id, str) or not current_tool_call_id:
        return warm_capture_refused(thread_id, "claude-code", "no_current_call")
    from core.fork_cc import _identity_from_live, capture_warm_source
    from subagents.cc_runtime_state import _LIVE

    async with _LIVE.lock(thread_id):
        if _LIVE.get(thread_id) is not live:
            return warm_capture_refused(thread_id, "claude-code", "live_changed")
        if live.tool_step_ids:
            return warm_capture_refused(
                thread_id, "claude-code", "tool_calls_outstanding",
            )
        identity = _identity_from_live(live)
        if identity is None:
            return warm_capture_refused(thread_id, "claude-code", "no_identity")
        provider_id, _invocation, _claude_dir, transcript_path = identity[:4]
        try:
            prefix_bytes = await asyncio.to_thread(
                _prefix_bytes_from_path,
                transcript_path,
                current_tool_call_id,
                context.warm_boundary,
                provider_id,
            )
        except OSError:
            return warm_capture_refused(
                thread_id, "claude-code", "transcript_unreadable",
            )
        if isinstance(prefix_bytes, str):
            return warm_capture_refused(thread_id, "claude-code", prefix_bytes)
        return await capture_warm_source(
            thread_id,
            context.boundary,
            target=context.target,
            binary=context.binary,
            live=live,
            prefix_bytes=prefix_bytes,
        )


async def capture_boundary_warm_source(
    context: Any,
    live: Any,
    thread_id: str,
):
    """Freeze a Claude initial/resume prefix through one exact durable boundary.

    The caller already holds ``_LIVE.lock(thread_id)``: this runs inside the
    input-acceptance transaction (``cc_feed.ensure_live_then_feed``). The lock
    is not reentrant, so it must not be acquired again here; the identity check
    below is the registry read the lock protects, and the caller's hold covers
    it. Re-acquiring it deadlocked every over-window Claude Code thread on
    2026-09-16.
    """
    if context.target.harness != "claude-code":
        return warm_capture_refused(thread_id, "claude-code", "target_not_claude_code")
    if context.boundary is None:
        return warm_capture_refused(thread_id, "claude-code", "no_boundary")
    if not isinstance(getattr(context, "warm_boundary", None), ClaudeTranscriptBoundary):
        return warm_capture_refused(thread_id, "claude-code", "no_warm_boundary")
    from core.fork_cc import _identity_from_live
    from subagents.cc_runtime_state import _LIVE

    if _LIVE.get(thread_id) is not live:
        return warm_capture_refused(thread_id, "claude-code", "live_changed")
    if getattr(live, "tool_step_ids", None):
        return warm_capture_refused(thread_id, "claude-code", "tool_calls_outstanding")
    identity = _identity_from_live(live)
    if identity is None:
        return warm_capture_refused(thread_id, "claude-code", "no_identity")
    return await _capture_boundary_identity(
        thread_id=thread_id,
        boundary=context.boundary,
        warm_boundary=context.warm_boundary,
        target=context.target,
        binary=context.binary,
        identity=identity,
        live=live,
    )


async def _capture_boundary_identity(
    *,
    thread_id: str,
    boundary,
    warm_boundary: ClaudeTranscriptBoundary,
    target,
    binary: str,
    identity,
    live: Any | None,
):
    from core.fork_cc import capture_warm_source

    provider_id, _invocation, _claude_dir, transcript_path = identity[:4]
    try:
        prefix_bytes = await asyncio.to_thread(
            _boundary_prefix_bytes_from_path,
            transcript_path,
            warm_boundary,
            provider_id,
        )
    except OSError:
        return warm_capture_refused(thread_id, "claude-code", "transcript_unreadable")
    if isinstance(prefix_bytes, str):
        return warm_capture_refused(thread_id, "claude-code", prefix_bytes)
    return await capture_warm_source(
        thread_id,
        boundary,
        target=target,
        binary=binary,
        live=live,
        prefix_bytes=prefix_bytes,
    )


async def capture_registered_boundary_warm_source(
    thread_id: str,
    boundary,
    warm_boundary: ClaudeTranscriptBoundary,
    *,
    target,
    binary: str,
    live: Any | None = None,
):
    """Prove and freeze a registered/live Claude prefix for non-pressure routes."""
    from core.fork_cc import (
        _identity_from_live,
        _live_identity,
        _registered_identity,
    )

    identity = _identity_from_live(live) if live is not None else await _live_identity(thread_id)
    if identity is None:
        identity = _registered_identity(thread_id)
    if identity is None:
        return warm_capture_refused(thread_id, "claude-code", "no_identity")
    return await _capture_boundary_identity(
        thread_id=thread_id,
        boundary=boundary,
        warm_boundary=warm_boundary,
        target=target,
        binary=binary,
        identity=identity,
        live=live,
    )
