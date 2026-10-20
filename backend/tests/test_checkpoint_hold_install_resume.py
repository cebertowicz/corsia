"""Real checkpoint storage, foreground wait, and same-Turn transport cutover."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.checkpoint_types import CheckpointRequest, ProviderCheckpointRequest, ProviderCheckpointResult
from core.turn_core import TurnIdentity
from codex_process_fixtures import bind_process_group


class _Pipe:
    def __init__(self):
        self.frames: list[bytes] = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


class _Process:
    """Only the OS process is synthetic; session feed and close stay real."""

    def __init__(self):
        self.stdin = _Pipe()
        self.returncode = None
        self.pid = 101

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


# Stop ends the attempt its own Turn is blocked on; any other stays independent.
_AFTER_STOP = {"owned": "cancelled", "independent": "running"}


async def _reach_wait(task, waiting):
    waiter = asyncio.create_task(waiting.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=5,
            return_when=asyncio.FIRST_COMPLETED)
        assert done, "foreground boundary reached neither a wait nor an exit"
        if task in done:
            task.result()
        assert waiting.is_set()
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def _seed(db, model):
    from test_checkpoint_external_resume import _seed_completed_turn_without_current_input

    _seed_completed_turn_without_current_input(db, "held-thread", model)
    db.execute("UPDATE messages SET content='ARCHIVE_NOT_REPLAYED' WHERE id='u1'")
    # The installed floor plus its old prose tail really exceeds the cap;
    # otherwise rebuilding that already-fitting selection is a valid exit.
    db.execute("UPDATE messages SET content=? WHERE id='a6'",
        ("FOLDED_HISTORY " * 16000,))
    rows = (
        ("u8", "user", "CURRENT_INPUT_RETAINED", {"turn_id": "turn-current"}, 8),
        ("a9", "assistant", "CURRENT_STEP_RETAINED", {
            "turn_id": "turn-current", "llm_tool_calls": [{
                "id": "current-call", "type": "function",
                "function": {"name": "test", "arguments": "{}"},
            }],
        }, 9),
        ("r10", "tool", "EXACT_DURABLE_TOOL_RESULT", {
            "turn_id": "turn-current", "assistant_step_id": "a9",
            "tool_call_id": "current-call",
        }, 10),
    )
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'held-thread',?,?,?,'t',?)",
        [(mid, role, content, json.dumps(meta), seq) for mid, role, content, meta, seq in rows],
    )
    db.execute("UPDATE threads SET next_message_seq=11 WHERE id='held-thread'")
    db.commit()


async def _start_checkpoint(monkeypatch, target, tmp_path, identity, *, owned=True):
    """Start one held attempt: this Turn's own, or an independent manual one."""
    from core import checkpoint_runtime as runtime

    release, waiting = asyncio.Event(), asyncio.Event()

    async def invoke(_request, _cancel):
        await release.wait()
        return ProviderCheckpointResult("NEW_CHECKPOINT_BODY", None)

    await_original = runtime.await_checkpoint

    async def observe_wait(checkpoint_id):
        waiting.set()
        return await await_original(checkpoint_id)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "await_checkpoint", observe_wait)
    # Test the foreground waiter, not the separate automatic idle-FIFO wake.
    monkeypatch.setattr(runtime, "_notify_completion", AsyncMock())
    await runtime.request_checkpoint(CheckpointRequest(
        id="new-checkpoint", thread_id="held-thread", history_epoch=0,
        previous_generation_id="floor", through_seq=7, through_message_id="r7",
        trigger="pressure" if owned else "manual", mode="warm",
        source_harness=target.harness,
        source_model=target.catalog_id, writer_harness=target.harness,
        writer_model=target.catalog_id,
        originating_turn_id=identity.turn_id if owned else None,
        first_included_seq=2, started_at="t2",
    ), ProviderCheckpointRequest(
        fork_id="new-checkpoint", thread_id="held-thread", target=target,
        instruction="checkpoint", reasoning_effort=None, working_dir=str(tmp_path),
    ))
    return release, waiting, await_original


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [None, "owned", "independent"])
@pytest.mark.parametrize("added_account", [False, True])
async def test_cc_held_turn_installs_checkpoint_and_resumes_or_stops_independently(
    isolated_db, monkeypatch, tmp_path, stop, added_account,
):
    from core import checkpoint_runtime as runtime
    from infra.database import get_db
    from registry.models import find_for_thread
    from registry.service import register_cc_session
    from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn
    from subagents.cc_stream import CCStreamSession
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary
    from tests.cc_runtime_fixtures import cc_target

    target = replace(cc_target(), context_window=32768)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_: str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # An added account resumes in its own config home, never the Default root.
    root = tmp_path / ("added-home" if added_account else "claude")
    if added_account:
        from connections import account_runtime

        monkeypatch.setattr(account_runtime, "launch_homes", lambda account_id, *, db=None:
            account_runtime.LaunchHomes("added", True, claude_dir=str(root)))
    db = get_db()
    _seed(db, target.catalog_id)
    register_cc_session("held-thread", "old-provider", checkpoint_generation_id="floor", db=db)
    db.commit()
    db.close()
    identity = TurnIdentity("turn-current", 0)
    old_session = CCStreamSession(binary="unused", session_id="old-provider",
        working_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))
    old_session._proc = _Process()
    old = _Live(session=old_session, parent_thread_id=None, name="old",
        catalog_model_id=target.catalog_id, context_window=target.context_window,
        corsia_turn=identity, last_call_usage={"input_tokens": 32760, "output_tokens": 8})
    assert activate_bound_turn(old, identity)
    scope = old.checkpoint_wait_scope
    _LIVE.put("held-thread", old)
    launches = []

    async def start(session):
        assert session is not old_session
        session._proc = _Process()
        launches.append(session)

    monkeypatch.setattr(CCStreamSession, "start", start)
    release, waiting, await_original = await _start_checkpoint(
        monkeypatch, target, tmp_path, identity, owned=stop != "independent",
    )
    task = asyncio.create_task(maintain_external_tool_boundary(
        old, "held-thread", "claude-code", complete_input_tokens=32788,
    ))
    try:
        await _reach_wait(task, waiting)
        assert scope.checkpoint_id == "new-checkpoint"
        assert not task.done()
        assert not launches and not old_session._proc.stdin.frames
        if stop:
            scope.request_stop()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert (await runtime._read("new-checkpoint")).status == _AFTER_STOP[stop]
        release.set()
        record = await asyncio.wait_for(await_original("new-checkpoint"), 5)
        assert record.status == ("cancelled" if stop == "owned" else "installed")
        if stop:
            assert not launches
        else:
            assert await asyncio.wait_for(task, 5)
            live = _LIVE.get("held-thread")
            assert live is not old and live.corsia_turn == identity
            assert live.checkpoint_wait_scope is scope
            assert live.turn_active and not old.turn_active
            assert len(launches) == 1 and live.session is launches[0]
            assert len(live.session._proc.stdin.frames) == 1
            assert old_session.returncode == -9
            transcript_path = Path(live.session.reconstructed_transcript_path)
            # The cold replay, CLAUDE_CONFIG_DIR and --resume all name one root.
            assert transcript_path.is_relative_to(root.resolve())
            assert live.session._build_env()["CLAUDE_CONFIG_DIR"] == str(root.resolve())
            cmd = live.session.build_cmd()
            assert transcript_path.stem == cmd[cmd.index("--resume") + 1]
            if added_account:
                assert not (tmp_path / "claude" / "projects").exists()
            transcript = transcript_path.read_text()
            seed = Path(live.seed_file).read_text()
            payload = transcript + seed + live.session._proc.stdin.frames[0].decode()
            assert "NEW_CHECKPOINT_BODY" in payload
            assert "CURRENT_INPUT_RETAINED" in payload
            assert "EXACT_DURABLE_TOOL_RESULT" in payload
            assert "ARCHIVE_NOT_REPLAYED" not in payload
            assert "FOLDED_HISTORY" not in payload
            assert 0 < live.session.tool_context.tokens <= target.context_window
        assert not old_session._proc.stdin.frames
        assert scope.checkpoint_id is None
        db = get_db()
        try:
            assert tuple(db.execute("SELECT id,status FROM active_turns WHERE id='turn-current'").fetchone()) == (identity.turn_id, "running")
            assert db.execute("SELECT content FROM messages WHERE id='r10'").fetchone()[0] == "EXACT_DURABLE_TOOL_RESULT"
            if not stop:
                registration = find_for_thread("held-thread", "claude-code", "session", db=db)
                assert json.loads(registration["metadata"])["checkpoint_generation_id"] == record.id
        finally:
            db.close()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown_checkpoint_tasks()
        for session in [old_session, *launches]:
            await session.terminate()
        _LIVE.reset()


class _RPCProcess(_Process):
    """Reply only at the wire; Codex startup, RPC reader and admission are real."""

    def __init__(self, rollout_path: Path):
        super().__init__()
        self.rollout_path = rollout_path
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stderr.feed_eof()
        self.requests: list[dict] = []
        self.stdin = self

    def write(self, frame):
        request = json.loads(frame)
        self.requests.append(request)
        if "id" not in request:
            return
        method = request["method"]
        if method == "thread/start":
            result = {"thread": {"id": "new-provider-thread", "path": str(self.rollout_path)}}
        elif method == "turn/start":
            result = {"turn": {"id": "new-provider-turn"}}
        else:
            assert method in {"initialize", "thread/inject_items"}, method
            result = {}
        self.stdout.feed_data(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")

    async def drain(self):
        pass

    def kill(self):
        super().kill()
        self.stdout.feed_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [None, "owned", "independent"])
async def test_codex_held_turn_installs_checkpoint_and_resumes_or_stops_independently(
    isolated_db, monkeypatch, tmp_path, stop,
):
    from core import checkpoint_runtime as runtime
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from registry.models import find_for_thread
    from registry.service import register_codex_thread
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS, _LIVE, _LiveCodex
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    target = replace(resolve_execution_target("codex/gpt-5-codex"), context_window=32768)
    source_home = tmp_path / "codex-source"
    source_home.mkdir()
    (source_home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": "synthetic-fixture-only"},
    }))
    (source_home / "models_cache.json").write_text(json.dumps({
        "models": [{"slug": "gpt-5-codex", "multi_agent_version": None}],
    }))
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_: str(tmp_path))
    db = get_db()
    _seed(db, target.catalog_id)
    register_codex_thread("held-thread", "old-provider", checkpoint_generation_id="floor", db=db)
    db.commit()
    db.close()
    identity = TurnIdentity("turn-current", 0)
    old_session = CodexAppServerSession(binary="unused", task=None, working_dir=str(tmp_path))
    old_session._proc = _Process()
    old_session.thread_id = "old-provider"
    old = _LiveCodex(session=old_session, parent_thread_id=None, name="old",
        catalog_model_id=target.catalog_id, context_window=target.context_window,
        corsia_turn=identity, codex_thread_id="old-provider")
    scope = old.checkpoint_wait_scope
    _LIVE.put("held-thread", old)
    _APP_SESSIONS["held-thread"] = old_session
    _CODEX_THREADS["held-thread"] = old.codex_thread_id
    launches = []

    async def spawn(*_args, **_kwargs):
        process = _RPCProcess(tmp_path / "rollout.jsonl")
        launches.append(process)
        bind_process_group(monkeypatch, process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    release, waiting, await_original = await _start_checkpoint(
        monkeypatch, target, tmp_path, identity, owned=stop != "independent",
    )
    task = asyncio.create_task(maintain_external_tool_boundary(
        old, "held-thread", "codex", complete_input_tokens=32788,
    ))
    try:
        await _reach_wait(task, waiting)
        assert scope.checkpoint_id == "new-checkpoint"
        assert not task.done() and not launches
        if stop:
            scope.request_stop()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert (await runtime._read("new-checkpoint")).status == _AFTER_STOP[stop]
        release.set()
        record = await asyncio.wait_for(await_original("new-checkpoint"), 5)
        assert record.status == ("cancelled" if stop == "owned" else "installed")
        if stop:
            assert not launches
        else:
            assert await asyncio.wait_for(task, 5)
            live = _LIVE.get("held-thread")
            assert live is not old and live.corsia_turn == identity
            assert live.checkpoint_wait_scope is scope
            assert len(launches) == 1 and live.session._proc is launches[0]
            methods = [request["method"] for request in launches[0].requests]
            assert methods == ["initialize", "initialized", "thread/start", "thread/inject_items", "turn/start"]
            payload = json.dumps(launches[0].requests)
            assert "NEW_CHECKPOINT_BODY" in payload
            assert "CURRENT_INPUT_RETAINED" in payload
            assert "EXACT_DURABLE_TOOL_RESULT" in payload
            assert "ARCHIVE_NOT_REPLAYED" not in payload
            assert "FOLDED_HISTORY" not in payload
            assert 0 < live.session.tool_context.tokens <= target.context_window
            assert old_session.returncode == -9
            db = get_db()
            try:
                assert tuple(db.execute("SELECT id,status FROM active_turns WHERE id='turn-current'").fetchone()) == (identity.turn_id, "running")
                registration = find_for_thread("held-thread", "codex", "thread", db=db)
                assert json.loads(registration["metadata"])["checkpoint_generation_id"] == record.id
            finally:
                db.close()
        assert scope.checkpoint_id is None
        assert not old_session._proc.stdin.frames
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown_checkpoint_tasks()
        for session in {old_session, _APP_SESSIONS.get("held-thread")} - {None}:
            session.set_callbacks(on_event=None, on_exit=None)
            await session.terminate()
        _LIVE.reset()
        _APP_SESSIONS.clear()
        _CODEX_THREADS.clear()
