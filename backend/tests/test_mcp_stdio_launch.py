"""A local app that fails to start leaves its masked stderr tail in the log."""
from __future__ import annotations

from contextlib import AsyncExitStack
import sys
import time

from loguru import logger
from mcp import StdioServerParameters
import pytest

from tools.mcp import connect_mcp_servers
from tools.registry import ToolRegistry

STORED_KEY = "plain-stored-value-42"
SHAPED_KEY = "xoxb-0000000000-not-a-real-slack-token"
CONFIGURED_KEY = "configured-key-value"

_SERVER = r'''
import json, sys
print("hello from server", file=sys.stderr, flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    result = {"tools": []} if request["method"] == "tools/list" else {
        "protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "ok", "version": "1"}}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''


@pytest.fixture
def warnings():
    records: list[str] = []
    sink = logger.add(
        lambda message: records.append(message.record["message"]), level="WARNING",
        filter=lambda record: record["name"] == "tools.mcp_stdio_launch",
    )
    yield records
    logger.remove(sink)


@pytest.fixture
def plain_launch(monkeypatch, tmp_path):
    """Run the app directly, with the stored key CORSIA would inject; Safehouse is proven below."""
    from tools import mcp, mcp_secrets, mcp_stdio_launch

    async def in_thread(function, *args):
        return function(*args)

    def parameters(_name, _config, command, args, env):
        profile = tmp_path / "profile.sb"
        profile.write_text("", encoding="utf-8")
        return StdioServerParameters(command=command, args=args, env=env), profile

    monkeypatch.setattr(mcp, "run_in_db", in_thread)
    monkeypatch.setattr(mcp_secrets, "resolve", lambda *_args: ({"STORED": STORED_KEY, "SLACK": SHAPED_KEY}, {}))
    monkeypatch.setattr(mcp_stdio_launch, "safehouse_stdio_parameters", parameters)


async def _connect(tmp_path, source: str, **config) -> dict:
    app = tmp_path / "app.py"
    app.write_text(source, encoding="utf-8")
    async with AsyncExitStack() as stack:
        return await connect_mcp_servers(
            {"local": {"command": sys.executable, "args": [str(app)], **config}}, ToolRegistry(), stack,
        )


_CRASH = r'''
import os, sys
print("starting with", os.environ["STORED"], os.environ["SLACK"], file=sys.stderr)
print("api_key=" + os.environ["API_KEY"], "home", os.environ["APP_HOME"], file=sys.stderr)
print("Error: Cannot find module '/missing/index.js'", file=sys.stderr)
sys.exit(1)
'''


async def test_failed_start_logs_the_masked_tail(plain_launch, warnings, tmp_path, capfd):
    errors = await _connect(tmp_path, _CRASH, env={"API_KEY": CONFIGURED_KEY, "APP_HOME": "/tmp/app-home"})

    assert "local" in errors
    [record] = warnings
    first, *_ = record.split("\n")
    assert first.startswith("MCP server 'local' connection ended by ")
    assert first.endswith("; its last 3 stderr lines (keys masked):")
    assert "Error: Cannot find module '/missing/index.js'" in record
    assert "/tmp/app-home" in record
    for secret in (STORED_KEY, SHAPED_KEY, CONFIGURED_KEY):
        assert secret not in record
    assert record.count("[REDACTED]") == 3
    # The backend's own stderr still gets the app's output as it did before.
    assert "Cannot find module" in capfd.readouterr().err


async def test_a_start_that_hangs_logs_its_tail_on_timeout(plain_launch, warnings, tmp_path):
    hang = 'import sys, time\nprint("waiting for the socket", file=sys.stderr, flush=True)\ntime.sleep(60)\n'
    started = time.monotonic()
    errors = await _connect(tmp_path, hang, connect_timeout=1)

    assert "local" in errors
    assert time.monotonic() - started < 10
    [record] = warnings
    assert record.endswith("(keys masked):\nwaiting for the socket")


async def test_the_tail_is_bounded(plain_launch, warnings, tmp_path):
    noisy = 'import sys\nfor n in range(50):\n    print(f"line {n}", "x" * (10000 if n == 45 else 0), file=sys.stderr)\nsys.exit(2)\n'
    await _connect(tmp_path, noisy)

    [record] = warnings
    lines = record.split("\n")[1:]
    assert len(lines) == 20
    assert lines[0].startswith("line 30") and lines[-1].startswith("line 49")
    assert max(len(line) for line in lines) == 200


async def test_an_app_that_prints_nothing_says_so(plain_launch, warnings, tmp_path):
    await _connect(tmp_path, "raise SystemExit(3)\n")

    [record] = warnings
    assert record.startswith("MCP server 'local' connection ended by ")
    assert record.endswith("; it wrote nothing to stderr")


async def test_a_healthy_app_logs_no_tail_and_keeps_its_stderr(plain_launch, warnings, tmp_path, capfd):
    errors = await _connect(tmp_path, _SERVER)

    assert errors == {}
    assert warnings == []
    assert "hello from server" in capfd.readouterr().err


async def test_a_healthy_app_corsia_rejects_names_corsia_as_the_cause(plain_launch, warnings, tmp_path):
    errors = await _connect(tmp_path, _SERVER, allowed_tools=["missing_tool"])

    assert "local" in errors
    [record] = warnings
    assert record.startswith(
        "MCP server 'local' connection ended by MCPConfigError: server did not advertise every allowed tool"
    )
    assert record.endswith("(keys masked):\nhello from server")


@pytest.mark.integration
async def test_failed_start_under_safehouse_logs_the_tail(monkeypatch, warnings, tmp_path):
    from tests.test_mcp_safehouse import _configure_roots, _require_safehouse

    _require_safehouse()
    _configure_roots(monkeypatch, tmp_path)
    errors = await _connect(tmp_path, _CRASH.replace('os.environ["STORED"], os.environ["SLACK"]', '"x"'),
                            env={"API_KEY": CONFIGURED_KEY, "APP_HOME": "/tmp/app-home"})

    assert "local" in errors
    [record] = warnings
    assert "Error: Cannot find module '/missing/index.js'" in record
    assert CONFIGURED_KEY not in record
