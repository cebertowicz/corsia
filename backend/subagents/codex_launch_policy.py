"""Validate a Codex session's tool surface before provider launch."""

from core.fork_tools import ForkToolBoundary
from subagents.codex_app_server_errors import CodexWriteSurfaceError


def assert_launch_write_surface(session) -> None:
    """Use an enforced fork kind or require the ordinary task write surface."""
    if session.control_only or session.task is None:
        return
    if isinstance(session.fork_tool_result_fits, ForkToolBoundary):
        # The boundary enforces the kind's admitted tools before execution.
        return
    if not session._is_write_capable():
        raise CodexWriteSurfaceError(
            "codex session was dispatched with a task but has no "
            "write-capable CORSIA tool; its harness cannot write to the "
            "workspace. Rebuild the tool registry before dispatch.",
            {
                "sandbox": session.sandbox_mode,
                "dynamic_tools": sorted(session._dynamic_tool_names()),
            },
        )
