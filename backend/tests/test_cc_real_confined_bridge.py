"""The stock Claude CLI reaches confined reads through the real loopback bridge."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import uuid
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

from cc_user_context_fixtures import InferencePeer
from contract.matrix_environment import guarded_command
from subagents.cc_invocation import CONFINEMENT_UNTRUSTED, build_claude_cmd
from subagents.cc_stream import CCStreamSession
from tools.confined_reads import UNTRUSTED_READ_TOOLS

_INSIDE = "inside-marker-bb921e"
_OUTSIDE = "outside-marker-a1b7c4"
_THREAD = "confined-cli-bridge"


def _seed(db, grant):
    from core.turn_core import BOOT_EPOCH
    from infra import thread_confinement
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) "
               "VALUES (?,'user-1','claude-code/sonnet','t','t')", (_THREAD,))
    thread_confinement.record(db, _THREAD, "untrusted", (str(grant),))
    db.commit()
    admission = admit_input(
        _THREAD, role="user", content="say ok", intent="apply_now", message_id="input-1",
        turn_id="turn-1", boot_epoch=BOOT_EPOCH, admitted_at="t", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id="write-1", started_at="t", db=db)
    settle_batch_write("write-1", outcome="accepted", db=db)
    return admission.batch.identity


def _call(case, base, grant):
    inside, outside = grant / "inside.txt", base / "outside.txt"
    return {
        "inside": ("mcp__corsia__read_file", {"path": str(inside)}),
        "outside": ("mcp__corsia__read_file", {"path": str(outside)}),
        "read": ("Read", {"file_path": str(inside)}),
        "grep": ("Grep", {"pattern": "marker", "path": str(base)}),
        "glob": ("Glob", {"pattern": "*.txt", "path": str(base)}),
        "write": ("mcp__corsia__write_file", {"path": str(grant / "written"), "content": "written"}),
        "bash": ("Bash", {"command": f"touch {base / 'bash-ran'}"}),
    }[case]


@pytest.mark.integration
@pytest.mark.parametrize("case", ["inside", "outside", "read", "grep", "glob", "write", "bash"])
async def test_stock_cli_confined_bridge(case, isolated_db, monkeypatch):
    """Only the inference peer is scripted; CLI, MCP subprocess and bridge are real."""
    from api import cc_attach
    from infra.config import WORKSPACE
    from infra.database import get_db
    from subagents.cc_confinement import launch_tools
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.harness_tool_bridge import configure_cc_tool_bridge
    from tests.vendor_account_fixtures import signed_in_default

    binary = shutil.which("claude")
    assert binary, "stock Claude CLI is required for this integration test"
    with tempfile.TemporaryDirectory(prefix="cc-bridge-", dir=WORKSPACE) as directory:
        base = Path(directory).resolve()
        grant = base / "grant"
        grant.mkdir()
        (grant / "inside.txt").write_text(_INSIDE)
        (base / "outside.txt").write_text(_OUTSIDE)
        home = base / "home"
        root = home / ".claude"
        root.mkdir(parents=True)
        tmp = base / "tmp"
        tmp.mkdir()
        peer = InferencePeer(_call(case, base, grant))
        app = FastAPI()
        app.include_router(cc_attach.router)
        bridge_calls = []

        @app.middleware("http")
        async def observe_bridge(request, call_next):
            if request.method == "POST" and request.url.path == f"/api/cc/_tool-bridge/{_THREAD}":
                bridge_calls.append(await request.json())
            return await call_next(request)

        @app.post("/{route:path}")
        async def inference(route: str, request: Request):
            raw = await request.body()
            if "/v1/messages" in f"/{route}" and "count_tokens" not in route:
                peer.bodies.append(raw.decode())
                return Response(peer._answer(json.loads(raw)), media_type="text/event-stream")
            return Response('{"input_tokens":1}', media_type="application/json")

        # One port permits the existing network guard to confine both real bridge
        # and fake inference traffic, including the CLI's MCP subprocess.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        monkeypatch.setenv("BACKEND_PORT", str(port))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        db = get_db()
        config = None
        process = None
        try:
            signed_in_default("claude-code", db=db)
            identity = _seed(db, grant)
            registry, definitions = launch_tools(None, True)
            session = CCStreamSession(
                binary=binary, session_id=str(uuid.uuid4()), model="claude-sonnet-4-6",
                working_dir=str(base), claude_dir=str(root), confinement=CONFINEMENT_UNTRUSTED,
            )
            live = _Live(session=session, parent_thread_id=None, name="cc", corsia_turn=identity,
                         turn_active=True, tool_definitions=definitions, context_window=200000)
            config = configure_cc_tool_bridge(live, thread_id=_THREAD, working_dir=str(base),
                                              registry=registry)
            session.mcp_config = config
            session._on_event = _make_on_event(live, _THREAD, lambda *_args: None, db)
            _LIVE.put(_THREAD, live)
            command = build_claude_cmd(session.invocation(), stream_input=False)
            env = session._build_env() | {
                "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(home),
                "TMPDIR": str(tmp), "CLAUDE_CONFIG_DIR": str(root),
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                "ANTHROPIC_API_KEY": "dummy-test-key", "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
            }
            process = await asyncio.create_subprocess_exec(
                *guarded_command(command, port), cwd=str(base), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            session._proc = process
            process.stdin.write(b"say ok\n")
            await process.stdin.drain()
            process.stdin.close()
            events = []

            async def read_stream():
                async for line in process.stdout:
                    event = json.loads(line)
                    events.append(event)
                    await session._dispatch_raw_event(event)

            await asyncio.wait_for(read_stream(), 120)
            stderr = (await process.stderr.read()).decode()
            await process.wait()
            assert process.returncode == 0, stderr[-1000:]
            assert peer.bodies, "CLI sent no inference request"
            [init] = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
            expected = {f"mcp__corsia__{name}" for name in UNTRUSTED_READ_TOOLS}
            assert set(init["tools"]) == expected
            for body in peer.bodies:
                assert {tool["name"] for tool in json.loads(body).get("tools", [])} == expected
            results = [body for body in peer.bodies if '"tool_result"' in body]
            if case == "inside":
                assert any(call["name"] == "read_file" and call["arguments"]["path"] == str(grant / "inside.txt")
                           for call in bridge_calls)
                assert any(_INSIDE in body for body in results)
            elif case == "outside":
                assert results and any("outside this confined worker's read paths" in body for body in results)
            else:
                assert not bridge_calls
                assert not any(_INSIDE in body for body in peer.bodies)
            assert _OUTSIDE not in json.dumps(events)
            assert _OUTSIDE not in stderr
            assert not any(_OUTSIDE in body for body in peer.bodies)
            assert not (grant / "written").exists()
            assert not (base / "bash-ran").exists()
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            _LIVE.pop(_THREAD, None)
            if config:
                Path(config).unlink(missing_ok=True)
            db.close()
            server.should_exit = True
            await serving
            sock.close()
