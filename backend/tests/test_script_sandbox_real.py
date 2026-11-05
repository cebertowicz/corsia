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
async def test_real_script_with_no_agents_account_runs_in_the_locked_state(sandbox) -> None:
    from connections.github_agent_home import GIT_CONFIG_SLOTS, LOCKED_HOME

    path = _script(
        sandbox,
        "locked.sh",
        f'test "$GH_CONFIG_DIR" = {shlex.quote(LOCKED_HOME)} && test "$GIT_CONFIG_COUNT" = {GIT_CONFIG_SLOTS}\n',
    )
    run = await _run(path)
    assert run.returncode == 0, run.stderr
    assert run.github_locked == "scripts get no GitHub login: there is no agents' GitHub account"


def _account(monkeypatch, state: str, home: Path | None = None) -> None:
    """Serve one fake agents' account view: absent, the person's own login, or verified."""
    from connections import github_agent
    from connections.github_agent import AgentAccountView

    views = {
        "absent": AgentAccountView("not_installed", absent=True),
        "person": AgentAccountView("wrong_account", "person_account", account_id="a",
                                   home=str(home) if home else None, binary="gh"),
        "ready": AgentAccountView("ready", account_id="a", home=str(home) if home else None, binary="gh",
                                  login="agents-bot", user_id=1),
    }
    monkeypatch.setattr(github_agent, "view", lambda db=None: views[state])
    monkeypatch.setattr(github_agent, "verify_now", lambda db=None: views[state])


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["absent", "person"])
async def test_locked_script_gh_api_fails_even_with_a_token_in_the_owners_env(sandbox, monkeypatch, state):
    _account(monkeypatch, state, Path(os.environ["HOME"]) / "agent-gh")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "synthetic-canary")
    path = _script(sandbox, "gh-api.sh",
                   "command -v gh >/dev/null || exit 99\n"
                   "GH_PROMPT_DISABLED=1 gh api user && exit 1\nexit 0\n")
    run = await _run(path, timeout=60)
    assert run.returncode != 99, "gh is not installed where the script runs, so this proves nothing"
    assert run.returncode == 0, run.stderr
    assert run.github_locked is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["absent", "person"])
async def test_locked_script_git_gets_no_credential_and_no_ssh_route(sandbox, monkeypatch, state):
    """The person's global helper is cleared, and an SSH agent holding a key can't push to GitHub."""
    home = Path(os.environ["HOME"])
    _account(monkeypatch, state, home / "agent-gh")
    (home / ".gitconfig").write_text(
        '[credential]\n\thelper = "!f() { echo username=person; echo password=synthetic-canary; }; f"\n')
    agent_dir = home / ".ssh" / "agent"
    agent_dir.mkdir(parents=True)
    key = home / "throwaway-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    socket_path = agent_dir / "s.corsia-test"
    started = subprocess.run(["ssh-agent", "-a", str(socket_path)], check=True, capture_output=True, text=True)
    agent_pid = next(part.split("=", 1)[1].rstrip(";") for part in started.stdout.split()
                     if part.startswith("SSH_AGENT_PID="))
    try:
        subprocess.run(["ssh-add", "-q", str(key)], check=True, env={**os.environ, "SSH_AUTH_SOCK": str(socket_path)})
        monkeypatch.setenv("SSH_AUTH_SOCK", str(socket_path))
        repo = sandbox / "repo"
        probe = (
            # Control: the person's global helper is visible, so clearing it below is what stops it.
            "git config --global --get-all credential.helper | grep -q synthetic-canary || exit 7\n"
            'printf "protocol=https\\nhost=github.com\\n\\n" | GIT_TERMINAL_PROMPT=0 git credential fill '
            "2>/dev/null | grep -q synthetic-canary && exit 1\n"
            'test "$(git ls-remote --get-url git@github.com:o/r.git)" = https://github.com/o/r.git || exit 2\n'
            'test "$(git ls-remote --get-url ssh://git@github.com/o/r.git)" = https://github.com/o/r.git || exit 3\n'
            f"git init -q {shlex.quote(str(repo))} && cd {shlex.quote(str(repo))} || exit 4\n"
            "GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t "
            "GIT_COMMITTER_EMAIL=t@example.invalid git commit -q --allow-empty -m t || exit 5\n"
            "GIT_TERMINAL_PROMPT=0 git push -q git@github.com:corsia-invalid/none.git HEAD:refs/heads/never && exit 6\n"
            "exit 0\n"
        )
        run = await _run(_script(sandbox, "git-locked.sh", probe), timeout=60)
    finally:
        subprocess.run(["kill", agent_pid], check=False)
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

    monkeypatch.setattr(channel, "default_broker", lambda: GithubReadBroker(
        lambda args, *, timeout: subprocess.CompletedProcess(args, 0, "from broker", "")))
    endpoints = ("pulls/1", "issues/1/comments", "pulls/1/reviews", "pulls/1/comments")
    commands = [f'corsia github read "repos/o/r/{endpoint}?page={page}" || exit $?'
                for endpoint in endpoints for page in (1, 2)]
    path = _script(sandbox, "broker.sh", "\n".join(commands) + "\n")
    run = await run_script(THREAD_ID, None, path, run_id="broker-real", timeout_seconds=60,
                           github_repo="o/r")
    assert run.returncode == 0, run.stderr
    assert run.stdout == b"from broker" * 8
    other = _script(sandbox, "other-repo.sh", 'corsia github read "repos/x/y/pulls/1"\n')
    run = await run_script(THREAD_ID, None, other, run_id="broker-other", timeout_seconds=60,
                           github_repo="o/r")
    assert run.returncode == 1 and run.stdout == b"" and b"repository_not_allowed" in run.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("github_repo", [None, "o/r"])
@pytest.mark.parametrize("state", ["absent", "person", "ready"])
async def test_script_cannot_read_person_gh_or_token_variables(sandbox, monkeypatch, state, github_repo):
    """Neither the person's nor the agents' gh home is exposed to scripts."""
    from tools import sandbox_config

    home = Path(os.environ["HOME"])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [sandbox.parent, home])
    (home / "readable-control").write_text("control")
    gh = home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_text("synthetic-canary")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GIT_ASKPASS"):
        monkeypatch.setenv(name, "synthetic-canary")
    agent_home = home / "agent-gh"
    agent_home.mkdir()
    (agent_home / "hosts.yml").write_text("synthetic-agent")
    _account(monkeypatch, state, agent_home)
    agent_hosts = shlex.quote(str(agent_home / "hosts.yml"))
    # The home sits inside a granted folder, so only the run's own deny can hide it.
    agent_probe = {
        "ready": f'if cat {agent_hosts} >/dev/null 2>&1; then exit 5; fi\n',
        "person": f'if cat {agent_hosts} >/dev/null 2>&1; then exit 6; fi\n',
        "absent": "",  # no agents' account, so no home of its own to hide
    }[state]
    probe = (f'cat {shlex.quote(str(home / "readable-control"))} >/dev/null || exit 3\n'
             'for name in GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GIT_ASKPASS; do '
             'test -z "${!name}" || exit 1; done\n'
             f'if cat {shlex.quote(str(gh / "hosts.yml"))} >/dev/null 2>&1; then exit 2; fi\n' + agent_probe)
    path = _script(sandbox, "canaries.sh", probe)
    run = await run_script(THREAD_ID, None, path, run_id="canaries", timeout_seconds=20,
                           github_repo=github_repo)
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
    async with channel.github_channel(THREAD_ID, "first-run", None, repo="o/r") as socket_path:
        probe = ("import socket,sys\n"
                 "s=socket.socket(socket.AF_UNIX)\n"
                 f"try: s.connect({str(socket_path)!r})\n"
                 "except OSError: sys.exit(0)\n"
                 "sys.exit(1)\n")
        path = _script(sandbox, "other.py", probe)
        run = await run_script(other, None, path, run_id="other-run", timeout_seconds=20)
        assert run.returncode == 0, run.stderr


@pytest.mark.asyncio
async def test_authenticated_script_gh_uses_private_forwarder(sandbox, monkeypatch):
    """Exercise the real script sandbox and gh client against a fake upstream."""
    from contextlib import asynccontextmanager
    import httpx
    from automation import script_sandbox
    from connections import github_agent

    _account(monkeypatch, "ready", Path(os.environ["HOME"]) / "agent-gh")
    monkeypatch.setattr(github_agent, "script_forwarder_credential", lambda view: "synthetic-secret")
    seen = []

    async def upstream(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"login": "fake"})

    original = script_sandbox.github_proxy

    @asynccontextmanager
    async def proxy(path, credential):
        async with original(path, credential, transport=httpx.MockTransport(upstream)):
            yield

    monkeypatch.setattr(script_sandbox, "github_proxy", proxy)
    path = _script(sandbox, "authenticated.sh", 'gh api user\n')
    run = await run_script(THREAD_ID, None, path, run_id="auth-gh", timeout_seconds=30, authenticated=True)
    assert run.returncode == 0, run.stderr
    assert b'"login":"fake"' in run.stdout
    assert seen == ["token synthetic-secret"]
    assert b"synthetic-secret" not in run.stdout + run.stderr


@pytest.mark.asyncio
async def test_agent_shell_cannot_connect_to_authenticated_run_socket(sandbox, monkeypatch, tmp_path):
    from automation import script_sandbox
    from connections.github_agent import AgentAccountView
    from tools import shell_guard

    async with script_sandbox._authenticated_client(THREAD_ID, AgentAccountView("needs_auth")) as (_, socket_path, _, _):
        code = ("import socket; s=socket.socket(socket.AF_UNIX); "
                f"s.connect({str(socket_path)!r}); print('connected')")
        command = f"python3 -c {shlex.quote(code)}"
        profile = tmp_path / "shell.sb"
        profile.write_text(shell_guard.execution_envelope_profile(
            command, sandbox, profile, leased=False, thread_id=THREAD_ID,
        ))
        process = await asyncio.create_subprocess_exec(
            *shell_guard.safehouse_argv(command, sandbox, profile, thread_id=THREAD_ID),
            cwd=sandbox, env=shell_guard.safehouse_subprocess_env(THREAD_ID),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode not in (0, 126), stderr
        assert b"connected" not in stdout
        assert b"Operation not permitted" in stderr, stderr


@pytest.mark.asyncio
async def test_another_script_cannot_connect_to_authenticated_run_socket(sandbox, monkeypatch):
    from automation import script_sandbox
    from connections.github_agent import AgentAccountView

    async with script_sandbox._authenticated_client(THREAD_ID, AgentAccountView("needs_auth")) as (_, socket_path, _, _):
        probe = ("import socket,sys\n"
                 "s=socket.socket(socket.AF_UNIX)\n"
                 f"try: s.connect({str(socket_path)!r})\n"
                 "except OSError: sys.exit(0)\n"
                 "sys.exit(1)\n")
        path = _script(sandbox, "other-auth.py", probe)
        run = await _run(path)
        assert run.returncode == 0, run.stderr
