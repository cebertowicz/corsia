"""Broker calls from a script channel use the existing read policy."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from automation import script_github_channel as channel
from connections.github_broker import GithubReadBroker


async def request(path, args):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps(args).encode() + b"\n")
    await writer.drain()
    size = int.from_bytes(await reader.readexactly(8), "big")
    result = json.loads(await reader.readexactly(size))
    writer.close()
    await writer.wait_closed()
    return result


@pytest.mark.asyncio
async def test_channel_vets_reads_and_ends_with_run(monkeypatch):
    calls = []

    def inner(args, *, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "body", "")

    monkeypatch.setattr(channel, "default_broker", lambda: GithubReadBroker(inner))
    await _check_channel(calls, os.environ.get("CORSIA_THREAD_ID", "test-thread"))


async def _check_channel(calls, thread_id):
    async with channel.github_channel(thread_id, "run", "rule") as path:
        assert path.stat().st_mode & 0o777 == 0o600
        for endpoint in ("pulls/7", "issues/7/comments", "pulls/7/reviews", "pulls/7/comments"):
            for page in (1, 2):
                response = await request(path, [f"repos/o/r/{endpoint}?per_page=100&page={page}"])
                assert response == {"code": 0, "stdout": "body", "stderr": ""}
        assert len(calls) == 8
        cli = Path(__file__).resolve().parents[1] / "cli.py"
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(cli), "github", "read", "repos/o/r/pulls/7?page=2",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, channel.SOCKET_ENV: str(path)},
        )
        stdout, stderr = await process.communicate()
        assert (process.returncode, stdout, stderr) == (0, b"body", b"")
        assert "method_not_allowed" in (await request(path, ["-X", "POST", "repos/o/r"]))["stderr"]
        assert "graphql_not_allowed" in (await request(path, ["graphql"]))["stderr"]
        assert "full_url" in (await request(path, ["https://example.com/"]))["stderr"]
        assert len(calls) == 9
    assert not path.exists()
    with pytest.raises(OSError):
        await asyncio.open_unix_connection(str(path))


@pytest.mark.asyncio
async def test_run_stages_the_documented_command_and_removes_it(monkeypatch, tmp_path):
    from automation import script_sandbox

    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda _: tmp_path)
    monkeypatch.setattr(channel, "default_broker", lambda: GithubReadBroker(
        lambda args, *, timeout: subprocess.CompletedProcess(args, 0, "body", "")))
    script = tmp_path / "run.sh"
    script.write_text('corsia github read "repos/o/r/pulls/7?page=2"\n')
    staged = []

    async def execute(_thread, _project, script, _run, _timeout, stdin, _trace, socket_path, client_dir):
        staged.append(client_dir)
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", script_sandbox.script_command(script, stdin, client_dir=client_dir),
            env={**os.environ, channel.SOCKET_ENV: str(socket_path)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert (process.returncode, stdout, stderr) == (0, b"body", b"")
        fallback_dir = tmp_path / "other-bin"
        fallback_dir.mkdir()
        fallback = fallback_dir / "corsia"
        fallback.write_text('#!/bin/sh\nprintf "%s" "$*"\n')
        fallback.chmod(0o700)
        script.write_text("corsia event emit example\n")
        env = {**os.environ, "PATH": f'{fallback_dir}:{os.environ["PATH"]}'}
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", script_sandbox.script_command(script, client_dir=client_dir),
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert (process.returncode, stdout, stderr) == (0, b"event emit example", b"")

    monkeypatch.setattr(script_sandbox, "_run_with_channel", execute)
    await script_sandbox._run(os.environ.get("CORSIA_THREAD_ID", "test-thread"), None,
                              script, "run", 10, None, (), None)
    assert not staged[0].exists()
