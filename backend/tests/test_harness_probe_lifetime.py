"""Harness CLI calls never outlive their caller, and unit tests never run an installed one (#2792, #732)."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time

import pytest

from vendor_account_fixtures import default_id

import connections.harness_models as harness_models
import connections.harness_process as harness_process
import connections.harness_refresh as harness_refresh
import connections.harness_usage_check as usage_checks


@pytest.fixture
def harness_spawns(monkeypatch, isolated_db):
    """Both harnesses read as installed; every subprocess start is recorded and refused."""
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    for provider in harness_models._ADAPTERS:
        upsert_connection(type=ConnectionType.CLI, provider=provider,
                          status=ConnectionStatus.AVAILABLE,
                          config={"binary": provider.removesuffix("-code")})
    monkeypatch.setattr(harness_models, "_CHECK_TASKS", {})
    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    monkeypatch.setattr(harness_refresh, "_PROBED", {})
    monkeypatch.setattr(harness_refresh, "_PENDING", {})
    monkeypatch.setattr(harness_refresh, "_notify_models_changed", lambda: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    spawned: list[tuple] = []

    async def _spawn(*argv, **_kwargs):
        spawned.append(argv)
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    return spawned


@pytest.mark.asyncio
async def test_request_paths_start_no_harness_cli_under_the_guard(harness_spawns):
    harness_refresh.schedule_stale_refresh()
    harness_refresh.schedule_refresh_for_selections(["codex/gpt-5.6-sol"])
    await asyncio.gather(*harness_models._CHECK_TASKS.values(), return_exceptions=True)
    await harness_refresh.prepare_selection("claude-code/never-seen")
    await harness_refresh.refresh_harness_catalogs(reason="startup")
    for provider in ("claude-code", "codex"):
        await usage_checks._check(provider, default_id(provider))

    assert harness_spawns == []


def _stuck_harness(tmp_path):
    """A CLI that never exits and whose child keeps its stdout open."""
    script = tmp_path / "stuck-harness"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep 30 & echo $! > {tmp_path}/grandchild\n"
        f"echo $$ > {tmp_path}/child\n"
        "trap '' TERM\n"
        "wait\n"
    )
    script.chmod(0o755)
    return str(script)


async def _pid(tmp_path, name: str) -> int:
    path = tmp_path / name
    for _ in range(200):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        await asyncio.sleep(0.01)
    raise AssertionError(f"{name} never started")


async def _gone(pid: int) -> bool:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def stuck_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_process, "REAP_SECONDS", 0.5)
    yield _stuck_harness(tmp_path)
    # Release the pipes while this loop can still finish their transport.
    grandchild = tmp_path / "grandchild"
    if grandchild.exists():
        try:
            os.kill(int(grandchild.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.sleep(0.2)


@pytest.mark.asyncio
@pytest.mark.real_harness_cli
async def test_cancelled_version_probe_kills_its_cli(tmp_path, stuck_harness):
    task = asyncio.create_task(harness_models.binary_version(stuck_harness))
    child = await _pid(tmp_path, "child")

    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert time.monotonic() - started < 2
    assert await _gone(child)


@pytest.mark.asyncio
@pytest.mark.real_harness_cli
async def test_shutdown_joins_a_check_stuck_in_a_probe(
    tmp_path, monkeypatch, stuck_harness,
):
    monkeypatch.setattr(harness_models, "_CHECK_TASKS", {})
    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    harness_models.start_background_check(
        "claude-code", lambda: harness_models.binary_version(stuck_harness),
    )
    child = await _pid(tmp_path, "child")

    await asyncio.wait_for(harness_models.shutdown_refresh_tasks(), timeout=3)

    assert harness_models._CHECK_TASKS == {}
    assert await _gone(child)


@pytest.mark.asyncio
async def test_timed_out_probe_is_reaped_despite_a_child_holding_its_pipes(
    tmp_path, stuck_harness,
):
    process = await asyncio.create_subprocess_exec(
        stuck_harness, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    child = await _pid(tmp_path, "child")

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        async with harness_process.killed_on_exit(process):
            await asyncio.wait_for(process.communicate(), timeout=0.2)

    assert time.monotonic() - started < 2
    assert await _gone(child)


@pytest.mark.asyncio
async def test_unit_tests_start_fake_but_never_installed_harness_clis(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    process = await asyncio.create_subprocess_exec(str(fake), stdout=asyncio.subprocess.PIPE)
    assert (await process.communicate())[0] == b"fake\n"

    for name in ("claude", "codex"):
        if shutil.which(name) is None:
            continue
        with pytest.raises(FileNotFoundError, match="#732"):
            await asyncio.create_subprocess_exec(name, "--version")


def _isolated_claude_call(binary: str, *, timeout_seconds: float):
    from llm import isolated_structured
    from llm.resolution import ExecutionTarget

    target = ExecutionTarget(
        catalog_id="claude-code/reviewer", harness="claude-code", provider="claude-code",
        backend="claude-code", transport_model="fable", expected_reported_model="fable",
        context_window=200_000, available=True, availability_reason=None,
        display_name="Exact Reviewer", supports_reasoning=True,
    )
    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy", user_prompt="{}", output_schema={"type": "object"},
        max_output_tokens=100, timeout_seconds=timeout_seconds,
    )
    return isolated_structured._invoke_claude_code_structured(
        target, request, binary=binary, clean_env=lambda: {"PATH": "/bin:/usr/bin"},
    )


@pytest.mark.asyncio
async def test_timed_out_isolated_claude_call_reaps_despite_a_child_holding_its_pipes(
    tmp_path, stuck_harness,
):
    from llm.isolated_structured import IsolatedStructuredTimeoutError

    started = time.monotonic()
    with pytest.raises(IsolatedStructuredTimeoutError):
        await asyncio.wait_for(_isolated_claude_call(stuck_harness, timeout_seconds=0.5), timeout=5)

    assert time.monotonic() - started < 3
    assert await _gone(await _pid(tmp_path, "child"))


@pytest.mark.asyncio
async def test_cancelled_isolated_claude_call_reaps_despite_a_child_holding_its_pipes(
    tmp_path, stuck_harness,
):
    """Loop close cancels a pending title call this way; it must not strand teardown (#732)."""
    task = asyncio.create_task(_isolated_claude_call(stuck_harness, timeout_seconds=60))
    child = await _pid(tmp_path, "child")

    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert time.monotonic() - started < 2
    assert await _gone(child)
