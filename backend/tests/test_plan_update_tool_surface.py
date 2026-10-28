"""Agents write their plan through `plan_update`, including on Codex threads.

The registry and catalog serve `plan_update` as the Plans writer. A Codex
provider thread keeps the dynamic tools it started with when it resumes, so the
v15 migration archives each Codex thread handle; this module proves such a
thread then starts a fresh provider thread whose dynamic tools are the current
ones.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from tests.event_routing_helpers import mk_session


def _registry(tmp_path):
    from subagents.harness_tool_bridge import build_tool_registry

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return build_tool_registry(str(workspace))


def test_plan_update_describes_itself_as_the_plan_update_tool(tmp_path):
    from tools.catalog import TOOL_CATALOG

    registry = _registry(tmp_path)
    tool = registry.get("plan_update")

    assert tool is not None
    assert tool.description.startswith("Update plan.")
    assert TOOL_CATALOG["plan_update"].category == "plans"


async def _start_names(dynamic_tools) -> tuple[str, list[str]]:
    """Start one app-server session with no provider handle; return method and tools."""
    from subagents.codex_app_server import CodexAppServerSession

    session = CodexAppServerSession(
        binary="codex", task=None, working_dir="/work/project",
        dynamic_tools=dynamic_tools,
    )
    calls: list[tuple[str, dict]] = []

    async def _request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        return {"thread": {"id": "provider-fresh"}} if method == "thread/start" else {}

    async def _notify(_method: str, _params: dict) -> None:
        return None

    session._request = _request
    session._notify = _notify
    await session._initialize()
    await session._start_or_resume_thread()
    method, params = calls[-1]
    return method, [spec["name"] for spec in params.get("dynamicTools", [])]


@pytest.mark.asyncio
async def test_a_codex_thread_whose_handle_was_reset_starts_fresh_with_plan_update(
    isolated_db, tmp_path,
):
    """The handle a resumed thread used is gone; the next start sends current tools."""
    from infra.migration_14_to_15_codex_handles import migrate_14_to_15
    from registry.service import register_codex_thread
    from subagents.codex_destination_render import _tool_surface
    from subagents.codex_runtime_state import _CODEX_THREADS
    from subagents.codex_runtime_support import restore_thread_id

    thread_id = mk_session("codex-before-v15")
    register_codex_thread(
        thread_id, "provider-before-v15", rollout_path="/rollouts/before-v15.jsonl",
    )
    try:
        assert await restore_thread_id(thread_id, None) == "provider-before-v15"
        _CODEX_THREADS.pop(thread_id, None)

        db = get_db()
        try:
            migrate_14_to_15(db)
            db.commit()
        finally:
            db.close()

        assert await restore_thread_id(thread_id, None) is None
    finally:
        _CODEX_THREADS.pop(thread_id, None)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _registry_for_start, dynamic_tools = _tool_surface(str(workspace), None, thread_id)
    method, names = await _start_names(dynamic_tools)

    assert method == "thread/start"
    assert "plan_update" in names
