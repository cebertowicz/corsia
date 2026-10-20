"""Checkpoint's fork policy and its parameterized tool executor."""

from __future__ import annotations

from typing import Any

from core.checkpoint_capture import capture_and_install, terminal_failure
from core.checkpoint_native_tools import CHECKPOINT_TOOL_NAME
from core.fork_runtime import ForkKind


def _checkpoint_instruction(request: Any) -> str:
    return request.instruction


def _native_step_limit(agent: Any) -> int:
    return agent.max_iterations


async def execute_checkpoint_tool(agent: Any, name: str, arguments: dict[str, Any],
                                  cancel_event: Any, request: Any) -> str:
    """Run the one checkpoint mutation in its checkpoint staging context."""
    from core.checkpoint_native_tools import await_or_cancel
    from infra.continuity_staging import checkpoint_execution_scope
    from infra.state.context import tool_invocation_context

    with checkpoint_execution_scope(request.checkpoint_id):
        with tool_invocation_context(None, checkpoint_id=request.checkpoint_id):
            return await await_or_cancel(agent.tools.execute(name, arguments), cancel_event)


CHECKPOINT_KIND = ForkKind(
    name="checkpoint",
    instruction=_checkpoint_instruction,
    tool_allowlist=frozenset({CHECKPOINT_TOOL_NAME}),
    tool_refusal="Native checkpoints may only use continuity_edit.",
    over_room_text="{excess} tokens over the remaining checkpoint room and was not sent; write the checkpoint now.",
    tool_executor=execute_checkpoint_tool,
    step_limits={"native": _native_step_limit, "claude-code": None, "codex": None},
    end_handler=capture_and_install,
    limit_or_failure_handler=terminal_failure,
)
