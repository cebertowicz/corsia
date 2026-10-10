"""Synthetic upstream proof against the installed stock CLI, with denied egress."""

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from codex_receipt_stock_fixtures import ResponsesPeer
from codex_runtime_fixtures import declare_codex_tool_step
from contract.matrix_environment import guarded_command, isolated_environment
from subagents.codex_app_server import CodexAppServerSession


CI_STARTUP_TIMEOUT_SECONDS = 30


async def start_with_ci_budget(
    session,
    *,
    timeout_seconds=CI_STARTUP_TIMEOUT_SECONDS,
    clock=time.monotonic,
    wait_for=asyncio.wait_for,
):
    """Start a real app-server with diagnostics sized for a shared CI host."""
    started_at = clock()
    try:
        return await wait_for(session.start(), timeout_seconds)
    except Exception as exc:
        elapsed_seconds = clock() - started_at
        stderr_tail = bytes(session._stderr)[-2000:].decode("utf-8", "replace")
        raise AssertionError(
            f"Codex app-server startup failed after {elapsed_seconds:.2f}s "
            f"(budget {timeout_seconds}s); stderr tail:\n{stderr_tail}"
        ) from exc


async def test_ci_startup_budget_separates_cold_launch_from_behavioral_waits():
    """A controllable clock proves the old 10 s budget fails while 30 s passes."""

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    class DelayedSession:
        _stderr = b"app-server cold-launch diagnostic"

        async def start(self):
            clock.now += 11
            return True

    clock = Clock()
    observed_budgets = []

    async def virtual_wait_for(awaitable, timeout_seconds):
        observed_budgets.append(timeout_seconds)
        result = await awaitable
        if clock() > timeout_seconds:
            raise asyncio.TimeoutError
        return result

    with pytest.raises(AssertionError, match="after 11.00s.*stderr tail") as error:
        await start_with_ci_budget(
            DelayedSession(), timeout_seconds=10, clock=clock, wait_for=virtual_wait_for
        )
    assert isinstance(error.value.__cause__, asyncio.TimeoutError)
    assert "app-server cold-launch diagnostic" in str(error.value)

    clock.now = 0
    assert await start_with_ci_budget(
        DelayedSession(), clock=clock, wait_for=virtual_wait_for
    )
    assert observed_budgets == [10, CI_STARTUP_TIMEOUT_SECONDS]


@pytest.mark.asyncio
async def test_ci_budget_tolerates_controlled_process_exit_delay(monkeypatch):
    """The shared CI lifecycle budget also covers bounded process exit."""

    class DelayedProcess:
        async def wait(self):
            return 0

    class DelayedProcessGroup:
        def __init__(self):
            self.observed_budgets = []

        async def wait(self, timeout):
            self.observed_budgets.append(timeout)
            if timeout < 2:
                raise TimeoutError

    session = CodexAppServerSession(binary="codex", task=None, control_only=True)
    process_group = DelayedProcessGroup()
    session._proc = DelayedProcess()
    session._process_group = process_group
    monkeypatch.setattr(session, "_request_terminate", lambda: None)

    with pytest.raises(TimeoutError):
        await session.terminate(timeout=1)

    await session.terminate(timeout=CI_STARTUP_TIMEOUT_SECONDS)
    assert process_group.observed_budgets == [1, CI_STARTUP_TIMEOUT_SECONDS]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["fresh", "warm", "resume", "fork", "code_mode"])
async def test_stock_direct_barrier_and_code_mode_bypass(tmp_path, monkeypatch, scenario):
    code_mode = scenario == "code_mode"
    binary = shutil.which("codex")
    assert binary, "stock Codex CLI is required for this explicitly selected integration test"
    fixture = ResponsesPeer(code_mode=code_mode, tool_number=1 if scenario in {"fresh", "code_mode"} else 2)
    server = await asyncio.start_server(fixture.respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = isolated_environment(tmp_path, tmp_path / "workspace", tmp_path / "state")
    callback_entered, callback_release = asyncio.Event(), asyncio.Event()
    events = []

    class StockSession(CodexAppServerSession):
        def build_cmd(self):
            command = super().build_cmd()
            catalog_index = next(
                index
                for index, value in enumerate(command)
                if value.startswith("model_catalog_json=")
            )
            assert command[catalog_index - 1] == "-c"
            del command[catalog_index - 1 : catalog_index + 1]
            if code_mode:
                command[command.index("features.code_mode=false")] = "features.code_mode=true"
            return guarded_command(command, port)

        def build_env(self):
            return env

    async def execute(*_args, **_kwargs):
        callback_entered.set()
        await callback_release.wait()
        return "synthetic result"

    async def on_event(event):
        events.append(event)
        if event.get("kind") == "tool_use":
            # Stands in for the runtime's _on_tool_use; execute_tool is replaced.
            declare_codex_tool_step(session, event["id"], persist=False)

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    # A paid launch admits the Default account of a real thread, whose home is
    # this isolated environment's Codex home.
    from infra.database import get_db

    monkeypatch.setattr("subagents.codex_env._default_app_server_home", lambda: Path(env["CODEX_HOME"]))

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('synthetic-thread','owner','codex/gpt-5.4','t','t')"
        )
        db.commit()
    finally:
        db.close()
    session_options = dict(
        binary=binary, task=None, working_dir=str(tmp_path), model="gpt-5.4",
        codex_home=env["CODEX_HOME"], corsia_thread_id="synthetic-thread",
        tool_registry=object(), on_event=on_event,
        dynamic_tools=[{"type": "function", "function": {
            "name": "continuity_edit", "description": "Synthetic tool", "parameters": {"type": "object"},
        }}], config_overrides=(
            'model_provider="fixture"',
            f'model_providers.fixture={{name="Local fixture",base_url="http://127.0.0.1:{port}/v1",wire_api="responses",requires_openai_auth=false}}',
            'analytics.enabled=false', 'check_for_update_on_startup=false',
        ),
    )
    session = StockSession(**session_options)
    try:
        await start_with_ci_budget(session)
        assert Path(session._tool_requests.rollout_path).is_relative_to(Path(env["CODEX_HOME"]))
        if scenario in {"warm", "resume", "fork"}:
            await asyncio.wait_for(session.start_turn("Seed synthetic history"), 10)
            seed_turn = session.active_turn_id
            await asyncio.wait_for(asyncio.shield(session._completed_turns[seed_turn]), 10)
            if scenario in {"resume", "fork"}:
                provider_id, source = session.thread_id, Path(session._tool_requests.rollout_path)
                await session.terminate(timeout=CI_STARTUP_TIMEOUT_SECONDS)
                if scenario == "fork":
                    from llm.codex_rollout_files import copy_codex_rollout_to_isolated_home
                    fork_home = tmp_path / "fork-home"
                    copy_codex_rollout_to_isolated_home(
                        source, source_thread_id=provider_id,
                        source_sessions_root=Path(env["CODEX_HOME"]) / "sessions",
                        isolated_home=fork_home, source_prefix_bytes=source.stat().st_size,
                    )
                    env["CODEX_HOME"] = str(fork_home)
                    session_options["codex_home"] = str(fork_home)
                session = StockSession(**session_options, codex_thread_id=provider_id)
                await start_with_ci_budget(session)
            events.clear()
        await asyncio.wait_for(session.start_turn("Use the synthetic tool"), 10)
        fixture.release.set()
        if code_mode:
            # A background exec can request another generation while the inner
            # direct RPC has no matching provider call group. This is why the
            # common managed invocation must disable code mode.
            await asyncio.wait_for(fixture.second_request.wait(), 10)
            assert not callback_entered.is_set()
        else:
            await asyncio.wait_for(callback_entered.wait(), 10)
            assert len(fixture.requests) == fixture.tool_number
            assert session.tool_context.tokens == 9700
            call_usage = [event for event in events if event["kind"] == "call_usage"]
            assert len(call_usage) == 1
            assert call_usage[0]["usage"]["cache_read_tokens"] == 2000
            callback_release.set()
            await asyncio.wait_for(fixture.second_request.wait(), 10)
            completed = session._completed_turns.get(session.active_turn_id)
            if completed is not None:
                await asyncio.wait_for(asyncio.shield(completed), 10)
            calls = [event for event in events if event["kind"] == "call_usage"]
            assert len(calls) == 2  # Early durable + late stdout still bill one first call.
            assert sum(event["usage"]["total_tokens"] for event in calls) == 19400
        assert fixture.errors == []
    finally:
        callback_release.set()
        await session.terminate(timeout=CI_STARTUP_TIMEOUT_SECONDS)
        server.close()
        await server.wait_closed()
        await fixture.close()
    assert session.returncode is not None
    assert not session._tool_requests.tasks
