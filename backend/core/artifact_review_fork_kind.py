"""Artifact-review fork policy and its reviewer-scoped tool executor."""

from __future__ import annotations

from typing import Any

from core.fork_runtime import ForkKind
from core.artifact_review_capture import review_end_handler, review_limit_or_failure_handler


REVIEW_TOOL_ALLOWLIST = frozenset({
    "read_file", "doc_comments_list", "doc_comment_create", "doc_feedback_send",
})


def _review_instruction(request: Any) -> str:
    return request.instruction


def _native_step_limit(agent: Any) -> int:
    return agent.max_iterations


async def execute_review_tool(
    agent: Any, name: str, arguments: dict[str, Any], cancel_event: Any, request: Any,
) -> str:
    """Run a permitted document tool as the reviewer, bound to this request."""
    from core.checkpoint_native_tools import await_or_cancel
    from doceditor.review_fork_context import review_fork_scope
    from infra.state.context import tool_invocation_context

    from infra.db_async import run_in_db
    from doceditor.review_fork_context import authorize_tool

    request_id = await run_in_db(authorize_tool, request, name, arguments)
    with review_fork_scope(request_id):
        with tool_invocation_context(None):
            return await await_or_cancel(agent.tools.execute(name, arguments), cancel_event)


ARTIFACT_REVIEW_KIND = ForkKind(
    name="artifact_review",
    instruction=_review_instruction,
    tool_allowlist=REVIEW_TOOL_ALLOWLIST,
    tool_refusal="Artifact reviews may only read the document, create review comments, and submit them.",
    over_room_text=(
        "{excess} tokens over the remaining review room and was not sent; "
        "leave your comments and call doc_feedback_send now."
    ),
    tool_executor=execute_review_tool,
    step_limits={"native": _native_step_limit, "claude-code": 3, "codex": 3},
    end_handler=review_end_handler,
    limit_or_failure_handler=review_limit_or_failure_handler,
)
