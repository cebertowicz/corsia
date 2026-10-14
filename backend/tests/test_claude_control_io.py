"""Producer-path contracts for Claude control-session diagnostic capture."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


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


def _barrier_harness(tmp_path, name: str, body: str) -> tuple[str, Path, Path]:
    ready = tmp_path / f"{name}-ready"
    release = tmp_path / f"{name}-release"
    binary = _control_harness(
        tmp_path, name,
        f"{body}\necho $$ > '{ready}'\nwhile [ ! -e '{release}' ]; do sleep 0.01; done",
    )
    return binary, ready, release


def _start_timeout_after_ready(monkeypatch, module, ready: Path) -> None:
    original_wait_for = asyncio.wait_for

    async def timeout_after_ready(awaitable, timeout):
        task = asyncio.ensure_future(awaitable)
        await _wait_for_path(ready)
        return await original_wait_for(task, timeout=0.1 if timeout == 30 else timeout)

    monkeypatch.setattr(module.asyncio, "wait_for", timeout_after_ready)


def _timeout_log(caplog) -> str:
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("claude_control_timeout")][-1]


def _usage_frame() -> list[dict]:
    return [{"type": "control_request", "request_id": "corsia-usage",
             "request": {"subtype": "get_usage"}}]


def test_control_log_scope_is_opaque_per_scoped_account_generation():
    from connections import claude_control_io

    account_id = "synthetic-account-id"
    receipt = type("Receipt", (), {"account_id": account_id, "account_revision": 4, "login_seq": 7})()
    context = type("Context", (), {"receipt": receipt})()

    scope = claude_control_io.log_scope(context)
    assert scope.startswith("scoped-")
    assert account_id not in scope
    assert claude_control_io.log_scope(None) == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "expected"), [
    ('{"response":{"request_id":"corsia-usage","subtype":"success"}}', True),
    ('{"response":{"request_id":"corsia-usage","subtype":"error"}}', True),
    ('{"response":{"request_id":"corsia-usage"', False),
    (None, False),
], ids=("usage-success", "usage-error", "malformed-usage", "no-usage-reply"))
async def test_timeout_logs_usage_reply_presence_before_cleanup(
        tmp_path, monkeypatch, caplog, reply, expected):
    from connections import claude_discovery

    body = f"printf '%s\\n' '{reply}'" if reply else ":"
    binary, ready, release = _barrier_harness(tmp_path, "usage", body)
    _start_timeout_after_ready(monkeypatch, claude_discovery, ready)

    with pytest.raises(RuntimeError, match="Claude Code get_usage timed out"):
        await claude_discovery.run_claude_control(binary, _usage_frame(), operation="get_usage")

    release.touch()
    message = _timeout_log(caplog)
    assert f"usage_reply_observed={expected}" in message
    assert "process_exited_at_timeout=False" in message
    assert "returncode_at_timeout=None" in message


@pytest.mark.asyncio
async def test_catalog_and_end_replies_do_not_imply_usage_reply(tmp_path, monkeypatch, caplog):
    from connections import claude_discovery

    catalog = '{"response":{"request_id":"corsia-model-list","subtype":"success"}}'
    end = '{"response":{"request_id":"corsia-end","subtype":"success"}}'
    binary, ready, release = _barrier_harness(
        tmp_path, "catalog-end", f"printf '%s\\n%s\\n' '{catalog}' '{end}'",
    )
    _start_timeout_after_ready(monkeypatch, claude_discovery, ready)
    frames = [
        {"type": "control_request", "request_id": "corsia-model-list",
         "request": {"subtype": "list_models"}},
        {"type": "control_request", "request_id": "corsia-usage",
         "request": {"subtype": "get_usage"}},
        {"type": "control_request", "request_id": "corsia-end",
         "request": {"subtype": "end_session"}},
    ]

    with pytest.raises(RuntimeError, match="Claude Code list_models timed out"):
        await claude_discovery.run_claude_control(binary, frames, operation="list_models")

    release.touch()
    message = _timeout_log(caplog)
    assert "catalog_reply_observed=True" in message
    assert "usage_reply_observed=False" in message
    assert "context_reply_observed=False" in message


@pytest.mark.asyncio
async def test_timeout_snapshots_an_exited_process_with_pipes_held(tmp_path, monkeypatch, caplog):
    from connections import claude_discovery, harness_process

    ready = tmp_path / "exited-ready"
    release = tmp_path / "exited-release"
    child = tmp_path / "exited-child"
    binary = _control_harness(
        tmp_path, "exited-parent",
        f"(while [ ! -e '{release}' ]; do sleep 0.01; done) & echo $! > '{child}'; : > '{ready}'; exit 0",
    )
    monkeypatch.setattr(harness_process, "REAP_SECONDS", 0.1)
    _start_timeout_after_ready(monkeypatch, claude_discovery, ready)

    with pytest.raises(RuntimeError, match="Claude Code get_usage timed out"):
        await claude_discovery.run_claude_control(binary, _usage_frame(), operation="get_usage")

    release.touch()
    await _wait_for_pid_exit(child)
    message = _timeout_log(caplog)
    assert "process_exited_at_timeout=True" in message
    assert "returncode_at_timeout=0" in message


@pytest.mark.asyncio
async def test_control_reader_preserves_invalid_and_fragmented_output(tmp_path):
    from connections import claude_discovery

    invalid = _control_harness(tmp_path, "invalid-bytes", "printf '\\377'")
    assert await claude_discovery.run_claude_control(
        invalid, _usage_frame(), operation="get_usage",
    ) == b"\xff"

    response = '{"response":{"request_id":"corsia-usage","subtype":"success"}}\n'
    fragmented = _control_harness(
        tmp_path, "fragmented",
        f"printf '%s' '{response[:27]}'; sleep 0.01; printf '%s' '{response[27:]}'",
    )
    assert await claude_discovery.run_claude_control(
        fragmented, _usage_frame(), operation="get_usage",
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
        large_stderr, _usage_frame(), operation="get_usage",
    ), timeout=3) == response.encode()

    nonzero = _control_harness(tmp_path, "nonzero", "printf 'synthetic failure' >&2; exit 3")
    with pytest.raises(RuntimeError, match="Claude Code get_usage failed: synthetic failure"):
        await claude_discovery.run_claude_control(nonzero, _usage_frame(), operation="get_usage")


@pytest.mark.asyncio
async def test_cancelled_control_reader_reaps_its_harness(tmp_path):
    from connections import claude_discovery

    binary, ready, _release = _barrier_harness(tmp_path, "cancelled", "trap '' TERM")
    task = asyncio.create_task(claude_discovery.run_claude_control(
        binary, _usage_frame(), operation="get_usage",
    ))
    await _wait_for_path(ready)
    assert not task.done(), "producer must still be running before cancellation"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    await _wait_for_pid_exit(ready)


@pytest.mark.asyncio
async def test_invalid_control_frame_does_not_spawn_a_process(monkeypatch):
    from connections import claude_discovery

    spawns = 0

    async def _spawn(*_args, **_kwargs):
        nonlocal spawns
        spawns += 1
        raise AssertionError("invalid frames must be rejected before spawning")

    monkeypatch.setattr(claude_discovery.asyncio, "create_subprocess_exec", _spawn)

    with pytest.raises(TypeError):
        await claude_discovery.run_claude_control(
            "synthetic", [{"unserializable": object()}], operation="get_usage",
        )

    assert spawns == 0
