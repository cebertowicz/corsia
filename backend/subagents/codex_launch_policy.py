"""Validate a Codex session's tool surface before provider launch."""

from core.fork_tools import ForkToolBoundary
from subagents.codex_app_server_errors import CodexWriteSurfaceError


def build_cmd(session) -> list[str]:
    """Build the governed provider command, including confined native-tool controls."""
    import json
    from pathlib import Path
    from subagents.codex_env import governed_codex_model_catalog_path
    from subagents.managed_launch import (
        codex_app_server_config_override_args, codex_app_server_governance_args,
    )

    cmd = [session.binary, "app-server", "--stdio", *codex_app_server_governance_args()]
    if not session.control_only:
        catalog_path = governed_codex_model_catalog_path(
            Path(session.codex_home) if session.codex_home else None,
            confined=session.confined,
        )
        cmd += ["-c", f"model_catalog_json={json.dumps(str(catalog_path))}"]
    if session.effort:
        cmd += ["-c", f"model_reasoning_effort={session.effort}"]
    cmd += codex_app_server_config_override_args(session.config_overrides)
    if session.confined:
        from subagents.codex_confinement import config_args

        cmd += config_args()
    return cmd


def assert_launch_write_surface(session) -> None:
    """Use an enforced fork kind or require the ordinary task write surface."""
    if getattr(session, "confined", False):
        from subagents.codex_confinement import assert_tools

        assert_tools(session.dynamic_tools)
        return
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
