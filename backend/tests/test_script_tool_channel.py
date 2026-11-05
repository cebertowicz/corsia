"""A script tool call is dispatched as its owner without a model turn."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from automation.script_tool_channel import _serve
from test_script_github_proxy import Writer
from test_tool_turn_identity_propagation import _CaptureTool
from tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("signed_in_defaults")


@pytest.mark.asyncio
async def test_channel_executes_as_owner_without_turn(isolated_db):
    db = sqlite3.connect(isolated_db)
    db.execute("INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at) "
               "VALUES ('script-owner','u','test/model','p','t','t')")
    db.commit()
    db.close()
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"name": "capture_turn", "arguments": {"value": "x"}}).encode())
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, thread_id="script-owner", registry=registry)
    result = json.loads(bytes(writer.data[8:]))
    assert result["stdout"] == "ok" and result["code"] == 0
    assert tool.calls == [{"value": "x", "_thread_id": "script-owner", "_thread_project_id": "p"}]
