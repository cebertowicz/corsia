"""Artifact-review fork policy and its reviewer-scoped tool executor."""

from __future__ import annotations

from typing import Any

from core.fork_runtime import ForkKind, native_iteration_limit
from core.artifact_review_capture import (
    preserve_on_shutdown, provider_failure, review_end_handler, review_limit_or_failure_handler,
)
from core.artifact_review_usage import bind_usage


REVIEW_TOOL_ALLOWLIST = frozenset({
    "read_file", "list_dir", "glob", "grep", "doc_comments_list", "doc_comment_create", "doc_feedback_send",
})


async def execute_review_tool(
    agent: Any, name: str, arguments: dict[str, Any], cancel_event: Any, request: Any,
) -> str:
    """Run a permitted document tool as the reviewer, bound to this request."""
    from core.fork_native_tools import await_or_cancel
    from doceditor.review_fork_scope import authorize_tool
    from infra.state.context import tool_invocation_context
    from infra.db_async import run_in_db
    from tools.host_action_admission import without_host_lease

    try:
        request_id = await run_in_db(authorize_tool, request, name, arguments)
    except PermissionError:
        return ARTIFACT_REVIEW_KIND.tool_refusal
    with tool_invocation_context(None), without_host_lease():
        return await await_or_cancel(
            agent.tools.execute(name, arguments, review_request_id=request_id), cancel_event,
        )


ARTIFACT_REVIEW_KIND = ForkKind(
    name="artifact_review",
    tool_allowlist=REVIEW_TOOL_ALLOWLIST,
    tool_refusal="Artifact reviews may only write comments and feedback on the requested document.",
    over_room_text=(
        "{excess} tokens over the remaining review room and was not sent; "
        "leave your comments and call doc_feedback_send now."
    ),
    tool_executor=execute_review_tool,
    # Use the same harness limits as other disposable forks.
    step_limits={"native": native_iteration_limit, "claude-code": None, "codex": None},
    end_handler=review_end_handler,
    limit_or_failure_handler=review_limit_or_failure_handler,
    usage_binder=bind_usage,
    shutdown_handler=preserve_on_shutdown,
    provider_failure=provider_failure,
)
