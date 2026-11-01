"""Contracts retained after removing temporary Claude timeout diagnostics."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from types import SimpleNamespace


def _account(tmp_path):
    """Control sessions run inside an account's home: there is no ambient one."""
    return SimpleNamespace(source_home=str(tmp_path / "account-home"))


async def _wait_for_path(path: Path) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"producer did not signal {path.name}")
        await asyncio.sleep(0.01)


async def _wait_for_pid_exit(path: Path) -> None:
    await _wait_for_path(path)
    pid = int(path.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"child {pid} survived cleanup")


def _control_harness(tmp_path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text("#!/bin/sh\ncat >/dev/null\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


def _barrier_harness(tmp_path, name: str, body: str) -> tuple[str, Path]:
    ready = tmp_path / f"{name}-ready"
    binary = _control_harness(
        tmp_path, name,
        f"{body}\necho $$ > '{ready}'\nwhile :; do sleep 0.01; done",
    )
    return binary, ready


def _usage_frame() -> list[dict]:
    return [{"type": "control_request", "request_id": "corsia-usage",
             "request": {"subtype": "get_usage"}}]


@pytest.mark.asyncio
async def test_control_timeout_preserves_error_and_reaps_its_harness(tmp_path, monkeypatch):
    from connections import claude_discovery

    binary, ready = _barrier_harness(tmp_path, "timeout", ":")
    original_wait_for = asyncio.wait_for

    async def timeout_after_ready(awaitable, timeout):
        task = asyncio.ensure_future(awaitable)
        await _wait_for_path(ready)
        return await original_wait_for(task, timeout=0.1 if timeout == 30 else timeout)

    monkeypatch.setattr(claude_discovery.asyncio, "wait_for", timeout_after_ready)
    with pytest.raises(RuntimeError, match="Claude Code get_usage timed out"):
        await claude_discovery.run_claude_control(binary, _usage_frame(), operation="get_usage", context=_account(tmp_path))
    await _wait_for_pid_exit(ready)


@pytest.mark.asyncio
async def test_control_reader_preserves_invalid_and_fragmented_output(tmp_path):
    from connections import claude_discovery

    invalid = _control_harness(tmp_path, "invalid-bytes", "printf '\\377'")
    assert await claude_discovery.run_claude_control(
        invalid, _usage_frame(), operation="get_usage", context=_account(tmp_path),
    ) == b"\xff"

    response = '{"response":{"request_id":"corsia-usage","subtype":"success"}}\n'
    fragmented = _control_harness(
        tmp_path, "fragmented",
        f"printf '%s' '{response[:27]}'; sleep 0.01; printf '%s' '{response[27:]}'",
    )
    assert await claude_discovery.run_claude_control(
        fragmented, _usage_frame(), operation="get_usage", context=_account(tmp_path),
    ) == response.encode()


@pytest.mark.asyncio
async def test_control_reader_drains_stderr_and_preserves_nonzero_error(tmp_path):
    from connections import claude_discovery

    response = '{"response":{"request_id":"corsia-usage","subtype":"success"}}\n'
    large_stderr = _control_harness(
        tmp_path, "large-stderr",
        f"dd if=/dev/zero bs=65536 count=2 2>/dev/null >&2; printf '%s' '{response}'",
    )
    assert await asyncio.wait_for(claude_discovery.run_claude_control(
        large_stderr, _usage_frame(), operation="get_usage", context=_account(tmp_path),
    ), timeout=3) == response.encode()

    nonzero = _control_harness(tmp_path, "nonzero", "printf 'synthetic failure' >&2; exit 3")
    with pytest.raises(RuntimeError, match="Claude Code get_usage failed: synthetic failure"):
        await claude_discovery.run_claude_control(nonzero, _usage_frame(), operation="get_usage", context=_account(tmp_path))


@pytest.mark.asyncio
async def test_cancelled_control_reader_reaps_its_harness(tmp_path):
    from connections import claude_discovery

    binary, ready = _barrier_harness(tmp_path, "cancelled", "trap '' TERM")
    task = asyncio.create_task(claude_discovery.run_claude_control(
        binary, _usage_frame(), operation="get_usage", context=_account(tmp_path),
    ))
    await _wait_for_path(ready)
    assert not task.done(), "producer must still be running before cancellation"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    await _wait_for_pid_exit(ready)


@pytest.mark.asyncio
async def test_invalid_control_frame_does_not_spawn_a_process(tmp_path, monkeypatch):
    from connections import claude_discovery

    spawns = 0

    async def _spawn(*_args, **_kwargs):
        nonlocal spawns
        spawns += 1
        raise AssertionError("invalid frames must be rejected before spawning")

    monkeypatch.setattr(claude_discovery.asyncio, "create_subprocess_exec", _spawn)

    with pytest.raises(TypeError):
        await claude_discovery.run_claude_control(
            "synthetic", [{"unserializable": object()}], operation="get_usage", context=_account(tmp_path),
        )

    assert spawns == 0
