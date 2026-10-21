"""Checkpoint's fork policy and its parameterized tool executor."""

from __future__ import annotations

from typing import Any

from core.checkpoint_capture import capture_and_install, terminal_failure
from core.checkpoint_native_tools import CHECKPOINT_TOOL_NAME
from core.fork_runtime import ForkKind, native_iteration_limit
from core.checkpoint_usage import bind_usage
from core.fork_types import ProviderForkRejected


async def execute_checkpoint_tool(agent: Any, name: str, arguments: dict[str, Any],
                                  cancel_event: Any, request: Any) -> str:
    """Run the one checkpoint mutation in its checkpoint staging context."""
    from core.checkpoint_native_tools import await_or_cancel
    from infra.continuity_staging import checkpoint_execution_scope
    from infra.state.context import tool_invocation_context

    with checkpoint_execution_scope(request.fork_id):
        with tool_invocation_context(None, checkpoint_id=request.fork_id):
            return await await_or_cancel(agent.tools.execute(name, arguments), cancel_event)


async def shutdown_checkpoint(fork_id: str) -> None:
    """Cancel the checkpoint's durable row before joining its provider task."""
    from infra import checkpoints
    from infra.db_async import run_in_db
    from infra.time import utc_now_iso

    await run_in_db(checkpoints.cancel, fork_id, ended_at=utc_now_iso())


def provider_failure(harness: str, detail: str) -> None:
    """Keep CC/Codex partial output; Native provider errors have no result body."""
    if harness == "native":
        raise ProviderForkRejected(detail)


CHECKPOINT_KIND = ForkKind(
    name="checkpoint",
    tool_allowlist=frozenset({CHECKPOINT_TOOL_NAME}),
    tool_refusal="Native checkpoints may only use continuity_edit.",
    over_room_text="{excess} tokens over the remaining checkpoint room and was not sent; write the checkpoint now.",
    tool_executor=execute_checkpoint_tool,
    step_limits={"native": native_iteration_limit, "claude-code": None, "codex": None},
    end_handler=capture_and_install,
    limit_or_failure_handler=terminal_failure,
    usage_binder=bind_usage,
    shutdown_handler=shutdown_checkpoint,
    provider_failure=provider_failure,
)
