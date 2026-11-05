"""A script tool call is dispatched as its owner without a model turn."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from automation.script_tool_channel import REQUEST_LIMIT, _serve, tool_channel
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
    reader.feed_data(json.dumps({"name": "capture_turn", "arguments": {"value": "x"}}).encode() + b"\n")
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, thread_id="script-owner", registry=registry)
    result = json.loads(bytes(writer.data[8:]))
    assert result["stdout"] == "ok" and result["code"] == 0
    assert tool.calls == [{"value": "x", "_thread_id": "script-owner", "_thread_project_id": "p"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("data, stderr", [
    (b"not-json\n", "Tool request failed.\n"),
    (b"x" * (REQUEST_LIMIT + 1) + b"\n", "Tool request is larger than 8 MiB.\n"),
])
async def test_bad_or_over_limit_request_returns_error(data, stderr):
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(data)
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, thread_id="owner", registry=ToolRegistry())
    assert json.loads(bytes(writer.data[8:])) == {"code": 1, "stdout": "", "stderr": stderr}


@pytest.mark.asyncio
async def test_channel_accepts_request_over_asyncio_default_limit(isolated_db):
    db = sqlite3.connect(isolated_db)
    db.execute("INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at) "
               "VALUES ('script-owner','u','test/model','p','t','t')")
    db.commit()
    db.close()
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    # Agent shells can bind only their own socket root; CI has no such restriction.
    thread = os.environ.get("CORSIA_THREAD_ID")
    if thread:
        from tools.unix_socket_policy import corsia_temp_root_for_bind
        socket_parent = corsia_temp_root_for_bind(thread)
    else:
        socket_parent = Path(tempfile.gettempdir())
    path = socket_parent / uuid.uuid4().hex[:8]
    value = "x" * (256 * 1024)
    async with tool_channel(path, "script-owner", registry=registry):
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(json.dumps({"name": "capture_turn", "arguments": {"value": value}}).encode() + b"\n")
        await writer.drain()
        size = int.from_bytes(await reader.readexactly(8), "big")
        result = json.loads(await reader.readexactly(size))
        writer.close()
        await writer.wait_closed()
    assert result == {"code": 0, "stdout": "ok", "stderr": ""}
    assert tool.calls[0]["value"] == value


@pytest.mark.asyncio
async def test_channel_registry_uses_owner_working_dir(monkeypatch):
    import automation.script_tool_channel as channel

    built: list[str | None] = []
    monkeypatch.setattr(channel, "resolve_spawn_working_dir", lambda thread_id, db=None: ("/projects/p", "p"))
    monkeypatch.setattr(channel, "build_tool_registry", lambda working_dir=None: built.append(working_dir) or ToolRegistry())
    await channel._owner_registry("script-owner")
    assert built == ["/projects/p"]
