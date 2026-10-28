"""A script's GitHub channel: only a repository-bound rule run gets one, it reads only
that repository through the existing read policy and the shared read budget, and
every failure reaches the log and the client with its cause."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from automation import script_github_channel as channel
from connections.github_broker import GithubReadBroker
from events.connectors.github_read_budget import budget

THREAD = os.environ.get("CORSIA_THREAD_ID", "test-thread")
CLI = Path(__file__).resolve().parents[1] / "cli.py"


@pytest.fixture(autouse=True)
def fresh_budget():
    budget.clear()
    yield
    budget.clear()


def _broker(monkeypatch, respond):
    calls = []

    def inner(args, *, timeout):
        calls.append(args)
        return respond(args)

    monkeypatch.setattr(channel, "default_broker", lambda: GithubReadBroker(inner))
    return calls


async def request(path, args):
    return await raw_request(path, json.dumps(args).encode() + b"\n")


async def raw_request(path, line: bytes):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(line)
    await writer.drain()
    size = int.from_bytes(await reader.readexactly(8), "big")
    result = json.loads(await reader.readexactly(size))
    writer.close()
    await writer.wait_closed()
    return result


async def _cli(path, *args, env=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(CLI), "github", "read", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {}), channel.SOCKET_ENV: str(path)},
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


@pytest.mark.asyncio
async def test_channel_vets_reads_and_ends_with_run(monkeypatch):
    calls = _broker(monkeypatch, lambda args: subprocess.CompletedProcess(args, 0, "body", ""))
    async with channel.github_channel(THREAD, "run", "rule", repo="o/r") as path:
        assert path.stat().st_mode & 0o777 == 0o600
        for endpoint in ("pulls/7", "issues/7/comments", "pulls/7/reviews", "pulls/7/comments"):
            for page in (1, 2):
                response = await request(path, [f"repos/o/r/{endpoint}?per_page=100&page={page}"])
                assert response == {"code": 0, "stdout": "body", "stderr": ""}
        assert len(calls) == 8
        assert await _cli(path, "repos/o/r/pulls/7?page=2") == (0, b"body", b"")
        assert "method_not_allowed" in (await request(path, ["-X", "POST", "repos/o/r"]))["stderr"]
        assert "graphql_not_allowed" in (await request(path, ["graphql"]))["stderr"]
        assert "full_url" in (await request(path, ["https://example.com/"]))["stderr"]
        assert len(calls) == 9
    assert not path.exists()
    with pytest.raises(OSError):
        await asyncio.open_unix_connection(str(path))


@pytest.mark.asyncio
async def test_a_channel_reads_only_its_bound_repository(monkeypatch):
    calls = _broker(monkeypatch, lambda args: subprocess.CompletedProcess(args, 0, "body", ""))
    allowed = ["repos/o/r", "repos/o/r/pulls/1", "/repos/O/R/pulls/1?page=2", "Repos/o/r/issues/1/comments"]
    refused = ["repos/other/r/pulls/1", "repos/o/other/pulls/1", "repos/o/r2", "repos/r/o", "repos/o/r.",
               "notifications", "user", "rate_limit", "repositories/123", "repositories/123/pulls/1"]
    async with channel.github_channel(THREAD, "run", "rule", repo="o/r") as path:
        for endpoint in allowed:
            assert (await request(path, [endpoint]))["code"] == 0, endpoint
        for endpoint in refused:
            response = await request(path, [endpoint])
            assert response["code"] == 1 and response["stdout"] == "", endpoint
            assert "GitHub read refused: repository_not_allowed" in response["stderr"], endpoint
    assert [call[-1].lstrip("/") for call in calls] == [e.lstrip("/") for e in allowed]


@pytest.mark.asyncio
async def test_nothing_a_script_sends_widens_the_bound_repository(monkeypatch):
    calls = _broker(monkeypatch, lambda args: subprocess.CompletedProcess(args, 0, "body", ""))
    async with channel.github_channel(THREAD, "run", "rule", repo="o/r") as path:
        # A request object naming a repository is not a request.
        forged = await raw_request(path, json.dumps({"repo": "x/y", "args": ["repos/x/y"]}).encode() + b"\n")
        assert "malformed GitHub read request" in forged["stderr"]
        # A second path, a repository flag or gh's own repo placeholders choose nothing.
        for args in (["repos/o/r/pulls/1", "repos/x/y/pulls/1"], ["--repo", "x/y", "repos/x/y"],
                     ["repos/{owner}/{repo}/pulls/1"]):
            assert (await request(path, args))["code"] == 1, args
        # Nor does the script's environment.
        code, stdout, stderr = await _cli(path, "repos/x/y/pulls/1",
                                          env={"GH_REPO": "x/y", "CORSIA_GITHUB_REPO": "x/y"})
        assert (code, stdout) == (1, b"") and b"repository_not_allowed" in stderr
    assert calls == []


@pytest.mark.parametrize("facts, repo", [
    ({"repo": "o/r"}, "o/r"),
    ({"repo": "Owner.1/re_po-2", "pr_number": 1}, "Owner.1/re_po-2"),
    # A worker result names its claimed event's repository; the model's result text is not read.
    ({"repo": "o/r", "result": json.dumps({"repo": "x/y"})}, "o/r"),
    ({"result": json.dumps({"repo": "x/y"})}, None),
    ({"code": "ok"}, None),
    ({"repo": "o/r/pulls"}, None), ({"repo": "../r"}, None), ({"repo": "o/.."}, None),
    ({"repo": "o"}, None), ({"repo": ""}, None), ({"repo": 5}, None), ({"repo": ["o/r"]}, None),
    (None, None), ("o/r", None),
])
@pytest.mark.parametrize("origin", ["connector", "platform"])
def test_the_bound_repository_is_a_recorded_events_repo_fact(facts, repo, origin):
    assert channel.bound_repository({"type": "x.y", "origin": origin, "facts": facts}) == repo


@pytest.mark.parametrize("origin", ["emitter", "agent", None, "Connector"])
def test_an_event_a_script_or_agent_wrote_binds_no_repository(origin):
    event = {"type": "qa.forged", "facts": {"repo": "other/private"},
             **({"origin": origin} if origin is not None else {})}
    assert channel.bound_repository(event) is None
    assert channel.bound_repository("o/r") is None


def _response(status: str, headers: dict[str, str], body: str) -> str:
    lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return f"HTTP/2.0 {status}\n{lines}\r\n{body}"


@pytest.mark.asyncio
async def test_every_read_asks_the_shared_budget_first_and_tells_it_after(monkeypatch):
    reset = int(time.time()) + 600
    body = '{"number": 1}\r\nsecond line\n'
    responses = [
        _response("200 OK", {"X-Ratelimit-Remaining": "4000", "X-Ratelimit-Reset": str(reset)}, body),
        _response("200 OK", {"X-Ratelimit-Remaining": "3999", "X-Ratelimit-Reset": str(reset)}, body),
        _response("403 Forbidden", {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": str(reset),
                                    "X-Ratelimit-Resource": "core"}, '{"message": "API rate limit exceeded"}'),
    ]
    calls = _broker(monkeypatch, lambda args: subprocess.CompletedProcess(
        args, 1 if responses[0].startswith("HTTP/2.0 403") else 0, responses.pop(0), ""))
    async with channel.github_channel(THREAD, "run", "rule", repo="o/r") as path:
        plain = await request(path, ["repos/o/r/pulls/1"])
        assert plain == {"code": 0, "stdout": body, "stderr": ""}  # the body, byte for byte
        assert budget.status()["remaining"] == 4000
        included = await request(path, ["-i", "repos/o/r/pulls/1"])
        assert included["stdout"].startswith("HTTP/2.0 200 OK\n") and included["stdout"].endswith(body)
        assert budget.status()["remaining"] == 3999
        limited = await request(path, ["repos/o/r/pulls/1"])
        assert limited["code"] == 1 and limited["stdout"] == '{"message": "API rate limit exceeded"}'
        assert budget.blocked_until() is not None  # the pollers now back off as well
        paused = await request(path, ["repos/o/r/pulls/2"])
        assert paused["code"] == 1 and "paused by the shared rate-limit budget" in paused["stderr"]
    assert len(calls) == 3 and all("-i" in call for call in calls)


@pytest.mark.asyncio
async def test_a_read_stays_out_of_the_notifications_reserve(monkeypatch):
    calls = _broker(monkeypatch, lambda args: subprocess.CompletedProcess(args, 0, "body", ""))
    budget.record(200, {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "10",
                        "x-ratelimit-reset": str(int(time.time()) + 600)})
    async with channel.github_channel(THREAD, "run", "rule", repo="o/r") as path:
        response = await request(path, ["repos/o/r/pulls/1"])
    assert "paused by the shared rate-limit budget" in response["stderr"] and calls == []


@pytest.mark.asyncio
async def test_malformed_requests_and_broker_failures_are_logged_and_the_client_gets_the_cause(
        monkeypatch, caplog):
    failures = iter([OSError("gh could not start"), subprocess.TimeoutExpired(["gh"], 30),
                     KeyError("broken parser")])

    def respond(_args):
        raise next(failures)

    _broker(monkeypatch, respond)
    caplog.set_level(logging.WARNING, logger=channel.__name__)
    async with channel.github_channel(THREAD, "run-x", "rule-x", repo="o/r") as path:
        for line in (b"not json\n", b'"repos/o/r"\n', b"[1]\n"):
            response = await raw_request(path, line)
            assert response["code"] == 1 and response["stderr"].startswith("malformed GitHub read request: ")
        os_error = await request(path, ["repos/o/r/pulls/1"])
        assert "GitHub read failed: OSError: gh could not start" in os_error["stderr"]
        timeout = await request(path, ["repos/o/r/pulls/1"])
        assert "GitHub read failed: TimeoutExpired" in timeout["stderr"]
        unexpected = await request(path, ["repos/o/r/pulls/1"])
        assert "GitHub read failed: KeyError: 'broken parser'" in unexpected["stderr"]
        refused = await request(path, ["repos/x/y"])
        code, _stdout, stderr = await _cli(path, "repos/o/r", "repos/o/r/pulls")
    assert "repository_not_allowed" in refused["stderr"]
    assert code == 1 and b"malformed_args" in stderr
    records = [record for record in caplog.records if record.name == channel.__name__]
    messages = [record.getMessage() for record in records]
    assert sum("malformed request" in m for m in messages) == 3
    assert any("gh could not start" in m for m in messages)
    assert any("TimeoutExpired" in m for m in messages)
    assert any("failed unexpectedly" in m and record.exc_info for m, record in zip(messages, records))
    assert sum("refused" in m for m in messages) == 2
    assert all("run=run-x" in m and "repo=o/r" in m for m in messages)


@pytest.mark.asyncio
async def test_the_client_reports_why_the_channel_failed(tmp_path):
    code, stdout, stderr = await _cli(tmp_path / "missing-socket", "repos/o/r")
    assert (code, stdout) == (1, b"")
    assert stderr.startswith(b"GitHub read channel is unavailable: ") and b"Error" in stderr
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(CLI), "github", "read", "repos/o/r",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={k: v for k, v in os.environ.items() if k != channel.SOCKET_ENV},
    )
    _stdout, stderr = await process.communicate()
    assert process.returncode == 1 and b"started from an event naming a repository" in stderr


@pytest.mark.asyncio
async def test_run_stages_the_documented_command_and_removes_it(monkeypatch, tmp_path):
    from automation import script_sandbox

    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda _: tmp_path)
    _broker(monkeypatch, lambda args: subprocess.CompletedProcess(args, 0, "body", ""))
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
    await script_sandbox._run(THREAD, None, script, "run", 10, None, (), None, "o/r")
    assert not staged[0].exists()


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """``run_script`` up to the spawn, recording each channel opened and each command and env spawned."""
    from automation import script_sandbox
    from host_access.processes import ForegroundProcessResult

    record = {"channels": [], "commands": [], "envs": [], "spawn": None}

    async def nothing(*_a, **_k):
        return None

    async def cwd(*_a, **_k):
        return str(tmp_path), None

    async def prepare(command, *_a):
        record["commands"].append(command)
        return None, ["true"]

    async def spawn(_argv, **kwargs):
        record["envs"].append(kwargs["env"])
        if record["spawn"] is not None:
            raise record["spawn"]
        return ForegroundProcessResult(None, b"", b"", 0)

    @asynccontextmanager
    async def github_channel(thread_id, run_id, rule_id, *, repo=None):
        record["channels"].append((rule_id, repo))
        yield tmp_path / "socket"

    monkeypatch.setenv(channel.SOCKET_ENV, "/inherited/socket")
    monkeypatch.setattr(script_sandbox, "_checked_environment", nothing)
    monkeypatch.setattr(script_sandbox, "load_shell_repo_policies", lambda *_a: ([], []))
    monkeypatch.setattr(script_sandbox, "_resolve_execution_cwd", cwd)
    monkeypatch.setattr(script_sandbox, "execution_envelope_for_consumed_action", lambda *_a, **_k: None)
    monkeypatch.setattr(script_sandbox, "prepare_execution_envelope", prepare)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda _thread: tmp_path)
    monkeypatch.setattr(script_sandbox, "github_channel", github_channel)
    monkeypatch.setattr(script_sandbox.PROCESS_OWNER, "spawn", spawn)
    record["script"] = tmp_path / "s.sh"
    return record


def test_only_a_repository_bound_run_gets_a_channel_and_client(spawned):
    from automation.script_sandbox import run_script

    for kwargs in ({}, {"stdin": b"{}", "trace": ("rule_a",), "rule_id": "rule_a"},
                   {"stdin": b"{}", "trace": ("rule_b",), "rule_id": "rule_b", "github_repo": "o/r"}):
        asyncio.run(run_script(THREAD, None, spawned["script"], run_id="r", timeout_seconds=5, **kwargs))
    assert spawned["channels"] == [("rule_b", "o/r")]
    assert [env.get(channel.SOCKET_ENV) for env in spawned["envs"]] == [
        None, None, str(spawned["script"].parent / "socket")]
    assert ["github-client-" in command for command in spawned["commands"]] == [False, False, True]


def test_a_failure_after_the_channel_is_up_keeps_its_own_cause(spawned):
    from automation.script_sandbox import run_script

    spawned["spawn"] = OSError("spawn refused")
    with pytest.raises(OSError, match="spawn refused") as raised:
        asyncio.run(run_script(THREAD, None, spawned["script"], run_id="r", timeout_seconds=5,
                               github_repo="o/r"))
    assert "channel" not in str(raised.value)


def test_a_channel_that_cannot_open_says_why(spawned, monkeypatch):
    from automation import script_sandbox

    @asynccontextmanager
    async def broken(*_args, **_kwargs):
        raise OSError("socket path too long")
        yield  # pragma: no cover

    monkeypatch.setattr(script_sandbox, "github_channel", broken)
    with pytest.raises(script_sandbox.ScriptSandboxError,
                       match="script GitHub channel unavailable: OSError: socket path too long"):
        asyncio.run(script_sandbox.run_script(THREAD, None, spawned["script"], run_id="r",
                                              timeout_seconds=5, github_repo="o/r"))
    assert spawned["envs"] == []
