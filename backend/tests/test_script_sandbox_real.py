"""Real Safehouse runs of a scheduled script (L3, macOS).

Runs real scripts through ``script_sandbox.run_script`` and a real
``ProcessOwner``. Follows the convention in ``test_shell_execution_envelopes``:
skipped inside an agent shell and where Safehouse is missing, and failing in
both cases when ``CORSIA_REQUIRE_SAFEHOUSE=1`` (the L3 job). The default
database is never used: ``isolated_db`` points the DB at ``tmp_path``, the
workspace is a short directory under the system temp root, and the process
owner's runtime directory is fresh.

Startup timings are written to the CI log and carried in the assertion message.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from automation.script_sandbox import STDOUT_CAP_BYTES, run_script

pytestmark = pytest.mark.integration

THREAD_ID = "script-sandbox-it"
#: A pathological Safehouse startup must fail visibly, not hang the job.
FOUR_CONCURRENT_WALL_SECONDS = 10.0


@pytest.fixture
def short_system_temp():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def sandbox(isolated_db, monkeypatch, tmp_path, short_system_temp):
    from tools import sandbox_config, shell_guard, unix_socket_policy

    required = os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1"
    if os.environ.get("CORSIA_THREAD_ID"):
        if required:
            pytest.fail("CORSIA_REQUIRE_SAFEHOUSE=1 cannot run inside an agent shell")
        pytest.skip("agent shells cannot prove nested Safehouse confinement")
    if shell_guard.shutil.which("safehouse") is None:
        if required:
            pytest.fail("CORSIA_REQUIRE_SAFEHOUSE=1 but Safehouse is unavailable")
        pytest.skip("Agent Safehouse is unavailable")
    from automation import script_sandbox
    from host_access.processes import ProcessOwner
    from infra.database import get_db

    workspace = short_system_temp / "w"
    home = short_system_temp / "h"
    directory = workspace / "scripts"
    for path in (directory, home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
    monkeypatch.setattr(
        unix_socket_policy.tempfile, "gettempdir", lambda: str(short_system_temp),
    )
    monkeypatch.setenv("HOME", str(home))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?, 'user', 't', 't')",
            (THREAD_ID,),
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        script_sandbox,
        "PROCESS_OWNER",
        ProcessOwner(
            runtime_dir=tmp_path / "runtime",
            term_grace_seconds=0.2,
            kill_grace_seconds=2.0,
        ),
    )
    return directory


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


async def _run(path: Path, *, timeout: int = 20):
    return await run_script(
        THREAD_ID, None, path, run_id=uuid.uuid4().hex, timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_real_script_runs_in_the_sandbox_with_a_turnless_cwd(sandbox) -> None:
    path = _script(
        sandbox, "reading.sh", "pwd\necho '{\"series\":\"cost\",\"value\":3}'\n",
    )
    run = await _run(path)
    lines = run.stdout.decode().splitlines()
    assert run.returncode == 0 and not run.timed_out, run.stderr
    assert Path(lines[0]).resolve() == sandbox.resolve()  # cwd is the script's dir
    assert lines[1] == '{"series":"cost","value":3}'


@pytest.mark.asyncio
async def test_real_script_reads_a_rule_runs_event_on_stdin_and_the_input_is_removed(sandbox) -> None:
    from tools.unix_socket_policy import corsia_temp_root_for_bind

    path = _script(sandbox, "event.sh", "cat\n")
    run = await run_script(
        THREAD_ID, None, path, run_id=uuid.uuid4().hex, timeout_seconds=20,
        stdin=b'{"type": "qa.route", "facts": {}}',
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == b'{"type": "qa.route", "facts": {}}'
    assert not list(corsia_temp_root_for_bind(THREAD_ID).glob("rule-input-*"))


@pytest.mark.asyncio
async def test_real_script_receives_no_agent_account_environment(sandbox) -> None:
    path = _script(
        sandbox,
        "no-credentials.sh",
        'test -z "$GH_CONFIG_DIR" && test -z "$GIT_CONFIG_COUNT"\n',
    )
    run = await _run(path)
    assert run.returncode == 0, run.stderr


@pytest.mark.asyncio
async def test_real_script_exit_code_and_stderr_are_reported(sandbox) -> None:
    path = _script(sandbox, "fails.sh", "echo oops >&2\nexit 7\n")
    run = await _run(path)
    assert run.returncode != 0 and not run.timed_out
    assert b"oops" in run.stderr


@pytest.mark.asyncio
async def test_real_script_past_its_timeout_is_killed(sandbox) -> None:
    path = _script(sandbox, "hangs.sh", "echo started\nsleep 60\n")
    started = time.monotonic()
    run = await _run(path, timeout=2)
    assert run.timed_out
    assert time.monotonic() - started < 20  # killed, not waited out


@pytest.mark.asyncio
async def test_real_script_output_is_capped_at_64kb(sandbox) -> None:
    path = _script(sandbox, "floods.sh", "yes | head -c 1000000\n")
    run = await _run(path)
    assert len(run.stdout) == STDOUT_CAP_BYTES + 1  # one past the cap marks overflow
    assert run.over_cap


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 4])
async def test_real_script_startup_cost_by_concurrency(
    sandbox, count, capsys,
) -> None:
    path = _script(sandbox, "noop.sh", "echo '{\"series\":\"x\",\"value\":1}'\n")
    started = time.monotonic()
    runs = await asyncio.gather(*(_run(path) for _ in range(count)))
    wall = time.monotonic() - started
    line = f"SAFEHOUSE_STARTUP concurrent={count} wall_seconds={wall:.3f}"
    with capsys.disabled():
        print(f"\n{line}")
    assert all(run.returncode == 0 for run in runs), line
    if count == 4:
        assert wall < FOUR_CONCURRENT_WALL_SECONDS, line


@pytest.mark.asyncio
async def test_script_cli_reads_broker(sandbox, monkeypatch):
    from automation import script_github_channel as channel
    from connections.github_broker import GithubReadBroker
    from tools import shell_guard

    monkeypatch.setattr(channel, "default_broker", lambda: GithubReadBroker(
        lambda args, *, timeout: subprocess.CompletedProcess(args, 0, "from broker", "")))
    backend = Path(__file__).resolve().parents[1]
    original = shell_guard.agent_tool_ro_dirs
    monkeypatch.setattr(shell_guard, "agent_tool_ro_dirs", lambda: [*original(), backend])
    cli = shlex.quote(str(backend / "cli.py"))
    python = shlex.quote(sys.executable)
    endpoints = ("pulls/1", "issues/1/comments", "pulls/1/reviews", "pulls/1/comments")
    commands = [f'{python} {cli} github read "repos/o/r/{endpoint}?page={page}" || exit $?'
                for endpoint in endpoints for page in (1, 2)]
    path = _script(sandbox, "broker.sh", "\n".join(commands) + "\n")
    run = await run_script(THREAD_ID, None, path, run_id="broker-real", timeout_seconds=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout == b"from broker" * 8


@pytest.mark.asyncio
@pytest.mark.parametrize("account_present", [False, True])
async def test_script_cannot_read_person_gh_or_token_variables(sandbox, monkeypatch, account_present):
    from connections import github_agent
    from connections.github_agent import AgentAccountView
    from tools import sandbox_config

    home = Path(os.environ["HOME"])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [sandbox.parent, home])
    (home / "readable-control").write_text("control")
    gh = home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_text("synthetic-canary")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "synthetic-canary")
    agent_home = home / "agent-gh"
    agent_home.mkdir()
    monkeypatch.setattr(github_agent, "view", lambda: AgentAccountView(
        "ready" if account_present else "not_installed", absent=not account_present,
        home=str(agent_home) if account_present else None))
    probe = (f'cat {shlex.quote(str(home / "readable-control"))} >/dev/null || exit 3\n'
             'for name in GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN; do '
             'test -z "${!name}" || exit 1; done\n'
             f'if cat {shlex.quote(str(gh / "hosts.yml"))} >/dev/null 2>&1; then exit 2; fi\n')
    path = _script(sandbox, "canaries.sh", probe)
    run = await run_script(THREAD_ID, None, path, run_id="canaries", timeout_seconds=20)
    assert run.returncode == 0, run.stderr


@pytest.mark.asyncio
async def test_other_thread_cannot_connect_to_channel(sandbox):
    from automation import script_github_channel as channel
    from infra.database import get_db

    other = "script-sandbox-other"
    db = get_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?, 'user', 't', 't')", (other,))
        db.commit()
    finally:
        db.close()
    async with channel.github_channel(THREAD_ID, "first-run") as socket_path:
        probe = ("import socket,sys\n"
                 "s=socket.socket(socket.AF_UNIX)\n"
                 f"try: s.connect({str(socket_path)!r})\n"
                 "except OSError: sys.exit(0)\n"
                 "sys.exit(1)\n")
        path = _script(sandbox, "other.py", probe)
        run = await run_script(other, None, path, run_id="other-run", timeout_seconds=20)
        assert run.returncode == 0, run.stderr
