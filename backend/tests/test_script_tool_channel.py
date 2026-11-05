"""A script tool call is dispatched as its owner without a model turn."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from automation.script_tool_channel import _serve


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

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
    reader.feed_data(json.dumps({"name": "capture_turn", "arguments": {"value": "x"}}).encode() + b"\n")
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, thread_id="script-owner", registry=registry)
    result = json.loads(bytes(writer.data[8:]))
    assert result["stdout"] == "ok" and result["code"] == 0
    assert tool.calls == [{"value": "x", "_thread_id": "script-owner", "_thread_project_id": "p"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"not-json\n", b"x" * 65537 + b"\n"])
async def test_bad_or_over_limit_request_returns_error(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, thread_id="owner", registry=ToolRegistry())
    assert json.loads(bytes(writer.data[8:])) == {"code": 1, "stdout": "", "stderr": "Tool request failed.\n"}


@pytest.mark.asyncio
async def test_channel_registry_uses_owner_working_dir(monkeypatch):
    import automation.script_tool_channel as channel

    built: list[str | None] = []
    monkeypatch.setattr(channel, "resolve_spawn_working_dir", lambda thread_id, db=None: ("/projects/p", "p"))
    monkeypatch.setattr(channel, "build_tool_registry", lambda working_dir=None: built.append(working_dir) or ToolRegistry())
    await channel._owner_registry("script-owner")
    assert built == ["/projects/p"]
