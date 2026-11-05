"""A script can invoke the registry as its owner without an active turn."""
from __future__ import annotations

import sqlite3

import pytest

from test_tool_turn_identity_propagation import _CaptureTool
from tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("signed_in_defaults")


@pytest.mark.asyncio
async def test_registry_runs_tool_for_thread_without_turn(isolated_db):
    from infra import state

    db = sqlite3.connect(isolated_db)
    db.execute(
        "INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at) "
        "VALUES ('no-turn-thread','u','test/model','p','t','t')",
    )
    db.commit()
    db.close()
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    context = state._current_thread_id.set("no-turn-thread")
    try:
        result = await registry.execute("capture_turn", {"value": "x"})
    finally:
        state._current_thread_id.reset(context)
    assert result == "ok"
    assert tool.calls == [{
        "value": "x", "_thread_id": "no-turn-thread", "_thread_project_id": "p",
    }]
