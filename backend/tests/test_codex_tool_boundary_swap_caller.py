"""Codex's tool-boundary swap survives retiring its own callback's process.

Issue #1867 counterpart: Codex runs the swap inside the managed callback task
of the app-server it replaces. Retiring that process ends its reader, whose
cleanup cancels and joins the session's callback tasks. The real reader,
callback registry, checkpoint install and swap run here; only the OS process
is synthetic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from core.turn_core import TurnIdentity
from codex_process_fixtures import bind_process_group
from test_checkpoint_hold_install_resume import _RPCProcess, _seed, _start_checkpoint


def _codex_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "codex-source"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": "synthetic-fixture-only"},
    }))
    (home / "models_cache.json").write_text(json.dumps({
        "models": [{"slug": "gpt-5-codex", "multi_agent_version": None}],
    }))
    monkeypatch.setenv("CODEX_HOME", str(home))


@pytest.mark.asyncio
async def test_codex_tool_boundary_swap_survives_its_callers_reader_cleanup(
    isolated_db, monkeypatch, tmp_path,
):
    from core import checkpoint_runtime as runtime
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from registry.service import register_codex_thread
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS, _LIVE, _LiveCodex
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    target = replace(resolve_execution_target("codex/gpt-5-codex"), context_window=32768)
    _codex_home(tmp_path, monkeypatch)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_: str(tmp_path))
    db = get_db()
    _seed(db, target.catalog_id)
    register_codex_thread("held-thread", "old-provider", checkpoint_generation_id="floor", db=db)
    db.commit()
    db.close()
    identity = TurnIdentity("turn-current", 0)
    old_session = CodexAppServerSession(
        binary="unused", task=None, working_dir=str(tmp_path),
        corsia_thread_id="held-thread", tool_registry=object(),
    )
    old_session._proc = _RPCProcess(tmp_path / "old-rollout.jsonl")
    old_session.thread_id = "old-provider"
    old_session._reader = asyncio.create_task(old_session._read_loop())
    old = _LiveCodex(session=old_session, parent_thread_id=None, name="old",
        catalog_model_id=target.catalog_id, context_window=target.context_window,
        corsia_turn=identity, codex_thread_id="old-provider")
    _LIVE.put("held-thread", old)
    _APP_SESSIONS["held-thread"] = old_session
    _CODEX_THREADS["held-thread"] = old.codex_thread_id
    launches: list[_RPCProcess] = []

    async def spawn(*_args, **_kwargs):
        launches.append(_RPCProcess(tmp_path / "rollout.jsonl"))
        bind_process_group(monkeypatch, launches[-1])
        return launches[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    release, _waiting, await_original = await _start_checkpoint(
        monkeypatch, target, tmp_path, identity,
    )
    release.set()
    assert (await asyncio.wait_for(await_original("new-checkpoint"), 5)).status == "installed"
    callbacks = old_session._tool_requests
    # Register the callback exactly as ToolRequests.dispatch does.
    caller = asyncio.create_task(maintain_external_tool_boundary(
        old, "held-thread", "codex", complete_input_tokens=32788,
    ))
    callbacks.tasks.add(caller)
    try:
        assert await asyncio.wait_for(asyncio.shield(caller), 10) is True
        assert old_session._reader.done() and old_session.returncode == -9
        live = _LIVE.get("held-thread")
        assert live is not old and live.corsia_turn == identity
        assert len(launches) == 1
        methods = [request["method"] for request in launches[0].requests]
        assert methods[-1] == "turn/start"
    finally:
        callbacks.tasks.discard(caller)
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await runtime.shutdown_checkpoint_tasks()
        for session in {old_session, _APP_SESSIONS.get("held-thread")} - {None}:
            session.set_callbacks(on_event=None, on_exit=None)
            await session.terminate()
        _LIVE.reset()
        _APP_SESSIONS.clear()
        _CODEX_THREADS.clear()
