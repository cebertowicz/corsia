"""Claude Code command construction for disposable fork kinds."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.fork_failures import ForkBinaryResolutionError
from core.fork_runtime import ForkKind
from subagents.cc_invocation import ClaudeInvocation, build_claude_cmd


def _limit_args(kind: ForkKind, request: Any) -> list[str]:
    limit = kind.step_limit("claude-code", request)
    return [] if limit is None else ["--max-turns", str(limit)]


def warm_command(
    source: Any,
    request: Any,
    kind: ForkKind,
    snapshot_id: str,
) -> list[str]:
    """Resume a frozen prefix without changing its immutable cached portion."""
    invocation = replace(
        source.invocation,
        binary=request.binary or source.invocation.binary,
        resume_session_id=snapshot_id,
    )
    return build_claude_cmd(
        invocation,
        stream_input=True,
        extra_args=["--fork-session", "--no-session-persistence", *_limit_args(kind, request)],
    )


def cold_command(request: Any, kind: ForkKind) -> list[str]:
    """Build the one-shot cold command with the kind's explicit turn bound."""
    if not request.binary:
        raise ForkBinaryResolutionError("Claude Code fork requires its harness binary")
    invocation = ClaudeInvocation(
        binary=request.binary,
        permission_mode="bypassPermissions",
        mcp_config=request.mcp_config,
        working_dir=request.working_dir,
        model=request.target.transport_model,
        effort=request.reasoning_effort,
    )
    return build_claude_cmd(
        invocation,
        stream_input=True,
        extra_args=["--no-session-persistence", *_limit_args(kind, request)],
    )
