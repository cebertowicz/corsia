"""A script's no-turn channel cannot use bash or read outside protected scope."""
import asyncio
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from automation.script_tool_channel import tool_channel


@pytest.mark.asyncio
@pytest.mark.usefixtures("signed_in_defaults")
async def test_no_turn_shell_and_outside_read_are_refused(isolated_db, tmp_path):
    db = sqlite3.connect(isolated_db)
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('probe-owner','u','test/model','t','t')")
    db.commit()
    db.close()
    outside = tmp_path / "agent-home" / "hosts.yml"
    outside.parent.mkdir()
    outside.write_text("probe-data")
    # Agent shells can bind only their own socket root; CI has no such restriction.
    thread = os.environ.get("CORSIA_THREAD_ID")
    if thread:
        from tools.unix_socket_policy import corsia_temp_root_for_bind
        socket_parent = corsia_temp_root_for_bind(thread)
    else:
        socket_parent = Path(tempfile.gettempdir())
    path = socket_parent / uuid.uuid4().hex[:8]
    async with tool_channel(path, "probe-owner"):
        async def request(name, arguments):
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(json.dumps({"name": name, "arguments": arguments}).encode() + b"\n")
            await writer.drain()
            size = int.from_bytes(await reader.readexactly(8), "big")
            response = json.loads(await reader.readexactly(size))
            writer.close()
            await writer.wait_closed()
            return response

        bash = await request("bash", {"command": "echo hi"})
        read = await request("read_file", {"path": str(outside)})
        assert bash["code"] == 1
        assert "shell execution requires an exact active thread and turn identity" in bash["stdout"]
        assert read["code"] == 1
        assert "Blocked by protected scope" in read["stdout"]
        assert "probe-data" not in read["stdout"]
