"""Stored CORSIA runtime and isolated stock CLI used by CC receipt proofs."""

import asyncio
import json
import shutil
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from cc_receipt_stock_fixtures import ClaudePeer
from cc_runtime_fixtures import cc_target
from contract.matrix_environment import guarded_command, isolated_environment
from test_checkpoint_hold_install_resume import _seed, _start_checkpoint


class StockClaudeRuntime:
    """Keep fixture state, transport cleanup and durable assertions in one place."""

    def __init__(self, scenario, root, monkeypatch):
        self.scenario, self.root, self.monkeypatch = scenario, root, monkeypatch
        self.binary = shutil.which("claude")
        assert self.binary, "stock Claude CLI is required for this explicitly selected integration test"
        self.target = replace(cc_target("sonnet"), context_window=32768,
                              transport_model="claude-sonnet-4-6", expected_reported_model="claude-sonnet-4-6")
        self.frames, self.executed, self.sessions, self.raw_results = [], [], [], []
        self.complete = asyncio.Event()
        self.release_checkpoint = None
        self.fixture = ClaudePeer(self.execute,
            input_tokens=40000 if scenario in {"cutover", "required_failure"} else 100,
            calls=2 if scenario == "slow_concurrent" else 1)

    def broadcast(self, _thread, frame):
        self.frames.append(frame)
        if frame["type"] in {"turn_complete", "turn_cancelled"}:
            self.complete.set()

    async def execute(self, body):
        from api.cc_attach import _execute_admitted_tool

        if self.scenario == "mcp_error_control":
            raise RuntimeError("MCP errors are consumable")
        return await _execute_admitted_tool(self.live, "held-thread", body)

    async def execute_registry(self, name, arguments):
        self.executed.append((name, arguments))
        return f"canonical fixture result {arguments['path']}"

    def _configure_transport(self, port):
        from subagents.cc_stream import CCStreamSession

        self.env = isolated_environment(self.root, self.root / "workspace", self.root / "state") | {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_API_KEY": "synthetic-local-only",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTO_COMPACT": "1",
        }
        self.monkeypatch.setenv("CLAUDE_CONFIG_DIR", self.env["CLAUDE_CONFIG_DIR"])
        mcp = json.dumps({"mcpServers": {"corsia": {"command": sys.executable,
            "args": [str(Path(__file__).resolve().parents[1] / "subagents/corsia_mcp_server.py")],
            "env": {"CORSIA_CC_TOOL_URL": f"http://127.0.0.1:{port}/tool",
                    "CORSIA_CC_TOOL_TOKEN": "synthetic-local-only"}}}})
        original_cmd, original_start = CCStreamSession.build_cmd, CCStreamSession.start
        original_dispatch = CCStreamSession._dispatch_raw_event

        def command(session):
            session.mcp_config = mcp
            return guarded_command(original_cmd(session), port)

        async def start(session):
            self.sessions.append(session)
            await original_start(session)

        async def dispatch(session, raw):
            if isinstance(raw, dict) and raw.get("type") == "result":
                self.raw_results.append(raw)
            await original_dispatch(session, raw)

        self.monkeypatch.setattr(CCStreamSession, "build_cmd", command)
        self.monkeypatch.setattr(CCStreamSession, "_build_env", lambda _self: self.env)
        self.monkeypatch.setattr(CCStreamSession, "start", start)
        self.monkeypatch.setattr(CCStreamSession, "_dispatch_raw_event", dispatch)

    async def exited(self, code, stderr):
        from subagents.cc_handle_close import finalize
        from subagents.cc_process_exit import handle_process_exit

        await handle_process_exit("held-thread", self.live, code, stderr,
                                  self.broadcast, finalize_callback=finalize)

    async def open(self):
        from types import SimpleNamespace
        from core.turn_core import TurnIdentity
        from infra.database import get_db
        from registry.service import register_cc_session
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn
        from subagents.cc_stream import CCStreamSession

        self.monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: self.target)
        self.monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_: str(self.root))
        self.monkeypatch.setattr("infra.state.broadcast_to_thread", self.broadcast)
        self.server = await asyncio.start_server(self.fixture.respond, "127.0.0.1", 0)
        self._configure_transport(self.server.sockets[0].getsockname()[1])
        self.session = CCStreamSession(binary=self.binary, session_id=str(uuid.uuid4()),
            model=self.target.transport_model, working_dir=str(self.root),
            claude_dir=self.env["CLAUDE_CONFIG_DIR"], corsia_thread_id="held-thread")
        self.identity = TurnIdentity("turn-current", 0)
        self.live = _Live(session=self.session, parent_thread_id=None, name="stock", corsia_turn=self.identity,
            tool_registry=SimpleNamespace(execute=self.execute_registry, get=lambda _name: None), tool_token="synthetic-local-only",
            catalog_model_id=self.target.catalog_id, expected_reported_model=self.target.expected_reported_model,
            context_window=self.target.context_window)
        activate_bound_turn(self.live, self.identity)
        _LIVE.put("held-thread", self.live)
        self.session._on_event = _make_on_event(self.live, "held-thread", self.broadcast, None)
        self.session._on_exit = self.exited
        db = get_db()
        try:
            _seed(db, self.target.catalog_id)
            register_cc_session("held-thread", self.session.session_id, checkpoint_generation_id="floor", db=db)
            db.commit()
        finally:
            db.close()
        await self._arrange_checkpoint()
        await self.session.start()
        await self.session.feed("Use the fixture tool.")
        await asyncio.wait_for(self.fixture.tool_entered.wait(), 15)

    async def _arrange_checkpoint(self):
        if self.scenario == "required_failure":
            from core.checkpoint_runtime import ForkNotAdmitted

            async def unavailable(*_a, **_k):
                raise ForkNotAdmitted("fixture required checkpoint unavailable")

            async def cold(*_a, **_k):
                return None

            self.monkeypatch.setattr("subagents.harness_checkpoint_resume._prepare_external_checkpoint", unavailable)
            self.monkeypatch.setattr("core.fork_cc_boundary.capture_tool_boundary_warm_source", cold)
        elif self.scenario == "cutover":
            self.release_checkpoint, self.waiting, self.await_checkpoint = await _start_checkpoint(
                self.monkeypatch, self.target, self.root, self.identity)

    async def close(self):
        from subagents.cc_runtime_state import _LIVE

        self.fixture.release.set()
        if self.release_checkpoint is not None:
            self.release_checkpoint.set()
        for session in self.sessions:
            await session.terminate()
        self.server.close()
        await self.server.wait_closed()
        await self.fixture.close()
        _LIVE.pop("held-thread", None)
