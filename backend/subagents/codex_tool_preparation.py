"""Prepare the Codex tool surface for one thread."""

from infra.db_async import run_in_db
from tools.connected_app_visibility import account_capabilities_for_thread


async def prepare_app_server_tools(session, spec):
    """Resolve once off-loop, or preserve the preflight's frozen definitions."""
    from subagents.codex_runtime_spawn import resolve_app_server_tools, validate_prepared_app_server_tools

    if spec.tool_registry is not None:
        return validate_prepared_app_server_tools(session, spec.tool_registry, spec.dynamic_tools)
    capabilities = await run_in_db(account_capabilities_for_thread, spec.thread_id)
    return resolve_app_server_tools(
        session, working_dir=spec.working_dir, tool_registry=None,
        dynamic_tools=spec.dynamic_tools, capabilities=capabilities,
    )
