"""Connected-app reach and trusted context assembly for tool execution."""

from copy import deepcopy
from typing import Any, Callable

from tools.base import Tool
from tools.tool_surface import connected_app_reach_refusal, mcp_app_id, mcp_tool_reaches_thread


async def mcp_reach_refusal(tool: Tool, thread_context: dict[str, Any]) -> str | None:
    """Refuse an unreachable connected app before any tool/session access."""
    from tools.mcp import MCPToolWrapper

    if not isinstance(tool, MCPToolWrapper):
        return None
    if mcp_app_id(tool) is None:
        return connected_app_reach_refusal()
    from infra.database import get_db
    from infra.db_async import run_in_db

    def reaches() -> bool:
        db = get_db()
        try:
            return mcp_tool_reaches_thread(tool, thread_context["id"], db=db)
        finally:
            db.close()

    return None if await run_in_db(reaches) else connected_app_reach_refusal()


def strip_reserved_context(params: dict[str, Any], reserved: Callable[[object], bool]) -> dict[str, Any]:
    """Copy provider arguments without backend-owned identity or lease keys."""
    return {key: value for key, value in deepcopy(params).items() if not reserved(key)}


def inject_context(
    params: dict[str, Any], thread_context: dict[str, Any], *, tool_name: str | None = None,
) -> dict[str, Any]:
    """Return execution params with backend-owned authoritative identity."""
    execution_params = dict(params)
    execution_params.update({
        "_thread_id": thread_context["id"],
        "_thread_project_id": thread_context.get("project_id"),
    })
    turn_identity = thread_context.get("_host_access_turn_identity")
    if turn_identity is not None:
        execution_params["_host_access_turn_identity"] = turn_identity
    if tool_name == "bash":
        from infra.state.context import _current_tool_invocation_id

        execution_params["_thread_invocation_id"] = _current_tool_invocation_id.get()
    return execution_params
