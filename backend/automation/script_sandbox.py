"""Run one scheduled script inside the same envelope as its owner's shell.

The shell builds a Safehouse profile and argv for each command, then spawns it
through ``PROCESS_OWNER`` under the thread and Turn identity. A script run has
no Turn, so it uses a synthetic one (``script:<run_id>``) and the unleased
envelope: it can never inherit a host-access lease. Every builder below is the
one ``BashTool.execute`` calls, so the two cannot drift apart.

A rule-started run whose stored event names a repository also gets a GitHub
read channel for that repository (``script_github_channel``) and the
``corsia github read`` client; every other run gets neither. The person's
``gh`` homes stay hidden from every run.

Each run hides the person's and agents' GitHub homes from the script itself.
Authenticated scripts use the owner tool channel to act as their agent.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from automation.script_github_channel import SOCKET_ENV as GITHUB_READ_SOCKET_ENV, github_channel
from automation.script_github_proxy import github_proxy
from automation.script_tool_channel import SOCKET_ENV, tool_channel
from connections import github_agent
from connections.github_agent import AgentAccountView, Cause
from connections.github_agent_home import configured_root, default_home
from events.trace_env import TRACE_ENV
from events.trace_env import encode as encode_trace
from host_access.processes import PROCESS_OWNER, ForegroundProcessResult
from infra.project_repo_policy import (
    RepoPolicyQueryError,
    list_registered_repo_policies,
)
from tools.sandbox_config import is_path_allowed, is_safehouse_available
from tools.shell_cwd import _resolve_execution_cwd
from tools.shell_envelope import (
    execution_envelope_for_consumed_action,
    prepare_execution_envelope,
)
from infra.db_async import run_in_db, run_off_db
from tools.shell_environment import safehouse_subprocess_env
from tools.shell_guard import (
    execution_envelope_profile,
    safehouse_argv,
    safehouse_version_error,
)
from tools.shell_policy import load_shell_repo_policies
from tools.sandbox_git import sandbox_literal
import tools.shell_sandbox as shell_sandbox
from tools.unix_socket_policy import corsia_temp_root, corsia_temp_root_for_bind

#: Stdout the runner will read. One extra byte is kept so a longer stream is seen.
STDOUT_CAP_BYTES = 64 * 1024
STDERR_CAP_BYTES = 8 * 1024
_INTERPRETERS = {
    ".py": "python3", ".sh": "bash", ".bash": "bash", ".js": "node", ".mjs": "node",
}


class ScriptSandboxError(RuntimeError):
    """The run could not start; its message is shown to the owner."""


@dataclass(frozen=True)
class ScriptRun:
    """What one finished run produced."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    #: Why the run had no GitHub login, in plain words; None when it ran as the agents' account.
    github_locked: str | None = None

    @property
    def over_cap(self) -> bool:
        return len(self.stdout) > STDOUT_CAP_BYTES


def script_command(path: Path, stdin: Path | None = None, *, client_dir: Path | None = None) -> str:
    """The one bash command line for a script: ``stdin`` or none, capped stdout and stderr.

    ``head`` ends a runaway script with SIGPIPE instead of letting the process
    owner hold its whole output in memory, and ``pipefail`` keeps the script's
    own exit code visible.
    """
    quoted = shlex.quote(str(path))
    interpreter = _INTERPRETERS.get(path.suffix)
    run = f"{interpreter} {quoted}" if interpreter else quoted
    source = shlex.quote(str(stdin)) if stdin is not None else "/dev/null"
    client_path = f'export PATH={shlex.quote(str(client_dir))}:"$PATH"; ' if client_dir else ""
    return (
        f"{client_path}set -o pipefail; {run} <{source} "
        f"2> >(head -c {STDERR_CAP_BYTES} >&2) | head -c {STDOUT_CAP_BYTES + 1}"
    )


async def _checked_environment() -> None:
    error = await shell_sandbox.startup_error(
        is_safehouse_available, safehouse_version_error, prefix='sandbox unavailable: ',
    )
    if error:
        raise ScriptSandboxError(error)


async def run_script(
    thread_id: str,
    project_id: str | None,
    script: Path,
    *,
    run_id: str,
    timeout_seconds: int,
    stdin: bytes | None = None,
    trace: tuple[str, ...] = (),
    rule_id: str | None = None,
    github_repo: str | None = None,
    authenticated: bool = False,
) -> ScriptRun:
    """Run ``script`` in the owner's protected envelope and return its capped output.

    ``stdin`` is written to a private file in the thread's own temp root, which
    the envelope grants read-write, and removed once the run ends. ``trace`` is a
    rule-started run's rule chain, handed to ``corsia event emit`` (``events.trace_env``).
    ``github_repo`` is the repository a rule run's claimed event names; only then
    does the run get a GitHub read channel, and only for that repository.
    """
    await _checked_environment()
    if stdin is None:
        return await _run(thread_id, project_id, script, run_id, timeout_seconds, None, trace,
                          rule_id, github_repo, authenticated)
    try:
        fd, name = tempfile.mkstemp(prefix="rule-input-", suffix=".json",
                                    dir=corsia_temp_root_for_bind(thread_id))
    except (OSError, RuntimeError) as exc:
        raise ScriptSandboxError(f"the event input could not be staged: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(stdin)
        return await _run(thread_id, project_id, script, run_id, timeout_seconds, Path(name), trace,
                          rule_id, github_repo, authenticated)
    finally:
        Path(name).unlink(missing_ok=True)


async def _run(
    thread_id: str, project_id: str | None, script: Path, run_id: str, timeout_seconds: int,
    stdin: Path | None, trace: tuple[str, ...], rule_id: str | None, github_repo: str | None,
    authenticated: bool,
) -> ScriptRun:
    view = await run_in_db(github_agent.view)
    if view.answer_pending:
        view = await run_off_db(github_agent.verify_now)
    if not authenticated:
        if github_repo is not None:
            async with _legacy_github_client(thread_id, run_id, rule_id, github_repo) as (socket, client):
                return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                               stdin, trace, None, None, None, client, view,
                                               github_read_socket=socket)
        return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                       stdin, trace, None, None, None, None, view)
    async with _authenticated_client(thread_id, view) as (socket_dir, tool_socket, config_dir, client_dir):
        return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                       stdin, trace, socket_dir, tool_socket, config_dir, client_dir, view)


@asynccontextmanager
async def _legacy_github_client(thread_id: str, run_id: str, rule_id: str | None, repo: str):
    """The repository-scoped read channel for rule-started scripts that are not authenticated.

    A bridge with one exit: Agent Substrate deletes it, the read socket, its environment
    name and ``cli_github``'s legacy branch in one change once the scripts that read through
    it (the GitHub screen's and the review gate's) run as authenticated scripts.
    """
    stack = AsyncExitStack()
    try:
        socket = await stack.enter_async_context(github_channel(thread_id, run_id, rule_id, repo=repo))
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="github-client-", dir=corsia_temp_root_for_bind(thread_id))))
        client = directory / "corsia"
        client.write_bytes((Path(__file__).resolve().parents[1] / "cli_github.py").read_bytes())
        client.chmod(0o700)
    except OSError as exc:
        await stack.aclose()
        raise ScriptSandboxError(f"script GitHub channel unavailable: {type(exc).__name__}: {exc}") from exc
    async with stack:
        yield socket, directory


@asynccontextmanager
async def _authenticated_client(thread_id: str, view: AgentAccountView):
    """Run-private sockets and a script-readable configuration containing no credential."""
    async with AsyncExitStack() as stack:
        socket_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="r", dir=corsia_temp_root())))
        tool_socket = socket_dir / "t"
        await stack.enter_async_context(tool_channel(tool_socket, thread_id))
        client_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="script-client-", dir=corsia_temp_root_for_bind(thread_id))))
        client = client_dir / "corsia"
        client.write_bytes((Path(__file__).resolve().parents[1] / "cli_github.py").read_bytes())
        client.chmod(0o700)
        config_dir = None
        if view.ready:
            github_socket = socket_dir / "g"
            await stack.enter_async_context(github_proxy(
                github_socket, lambda: github_agent.script_forwarder_credential(view),
            ))
            config_dir = client_dir / "gh"
            config_dir.mkdir(mode=0o700)
            (config_dir / "config.yml").write_text(f"http_unix_socket: {github_socket}\n")
            # gh won't run without a stored login; the forwarder adds the real one. The key is split
            # so credential scanners don't take this placeholder for a secret.
            (config_dir / "hosts.yml").write_text(
                "github.com:\n    user: script\n    oauth_" + "token: placeholder\n"
            )
        yield socket_dir, tool_socket, config_dir, client_dir


#: One line per account cause: what is wrong, and who fixes it or what CORSIA does next.
_NO_LOGIN: dict[Cause, str] = {
    "read_failed": "CORSIA couldn’t read the agents' GitHub account settings and reads them again on the next run",
    "multiple_accounts": "there are several agents' GitHub accounts; the person needs to remove all but one",
    "github_executable_missing": "GitHub CLI for the agents' GitHub account is missing; install it again",
    "signed_out": "the agents' GitHub account is signed out; sign it in again",
    "credential_missing": "the saved sign-in for the agents' GitHub account is missing; sign it out and in again",
    "token_not_in_home": ("the sign-in for the agents' GitHub account isn't saved in the account's own settings; "
                          "sign it out and in again"),
    "token_rejected": "GitHub rejected the sign-in for the agents' GitHub account; sign it out and in again",
    "verify_failed": ("CORSIA couldn’t confirm the agents' GitHub account with GitHub and checks again "
                      "when a script next runs"),
    "verifying": "CORSIA is still checking the agents' GitHub account; a later run uses the answer",
    "own_login_unknown": ("CORSIA doesn't know the person's own GitHub login, so it can't tell the agents' "
                          "account from it; it checks again on a later run, and if the person isn't signed in "
                          "to GitHub CLI on this computer, they need to sign in"),
    "person_account": ("the agents' GitHub sign-in is the person's own GitHub account; sign it out and sign "
                       "in as the agents' own account"),
}
_NOT_READY = "the agents' GitHub account isn't ready"
_UNAPPROVED = ("scripts get no GitHub login: this script isn't approved to use the agents' GitHub account; "
               "schedule it with authenticated=true so the person can approve it")


def github_lock_reason(view: AgentAccountView, *, approved: bool = True) -> str | None:
    """Why a run gets no GitHub login; None only for an approved run on a verified account."""
    if view.ready:
        return None if approved else _UNAPPROVED
    if view.absent:
        why: str | None = "there is no agents' GitHub account"
    else:
        why = _NO_LOGIN.get(view.cause) if view.cause else None
    return f"scripts get no GitHub login: {why or _NOT_READY}"


def _script_gh_homes(view: AgentAccountView) -> set[Path]:
    """The person's and agents' ``gh`` homes are hidden from every run."""
    paths = {default_home(), Path.home() / ".config" / "gh"}
    if os.environ.get("XDG_CONFIG_HOME"):
        paths.add(Path(os.environ["XDG_CONFIG_HOME"]) / "gh")
    if view.home:
        paths.add(Path(view.home))
    if root := configured_root():
        paths.add(root)
    return {path.resolve(strict=False) for path in paths}


def _script_profile(view: AgentAccountView, socket_dir: Path | None = None):
    hidden = _script_gh_homes(view)

    def build(*args, **kwargs) -> str:
        profile = execution_envelope_profile(*args, **kwargs)
        for path in hidden:
            profile += f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))\n'
        if socket_dir is not None:
            root = sandbox_literal(socket_dir.resolve())
            profile += f'(allow file-read* (subpath "{root}"))\n'
            profile += f'(allow network-outbound (remote unix-socket (subpath "{root}")))\n'
        return profile
    return build


async def _run_with_channel(
    thread_id: str, project_id: str | None, script: Path, run_id: str, timeout_seconds: int,
    stdin: Path | None, trace: tuple[str, ...], socket_dir: Path | None, socket_path: Path | None,
    config_dir: Path | None, client_dir: Path | None, view: AgentAccountView,
    *, github_read_socket: Path | None = None,
) -> ScriptRun:
    env = safehouse_subprocess_env(thread_id)
    # Keep the shell's locked Git configuration, without exposing either gh home.
    env.update(github_agent.locked_env())
    env.pop(SOCKET_ENV, None)
    env.pop(GITHUB_READ_SOCKET_ENV, None)
    if github_read_socket is not None:
        env[GITHUB_READ_SOCKET_ENV] = str(github_read_socket)
    if socket_path is not None:
        env[SOCKET_ENV] = str(socket_path)
    if config_dir is not None:
        env["GH_CONFIG_DIR"] = str(config_dir)
        env["GH_PROMPT_DISABLED"] = "1"
    if trace:
        env[TRACE_ENV] = encode_trace(trace)
    try:
        policies, grant_policies = await asyncio.to_thread(
            load_shell_repo_policies, list_registered_repo_policies, False, project_id,
        )
    except RepoPolicyQueryError as exc:
        raise ScriptSandboxError(
            f"repository policy enforcement is unavailable: {exc}"
        ) from exc
    cwd, denial = await _resolve_execution_cwd(
        str(script.parent), str(script.parent),
        leased=False, policies=policies, thread_id=thread_id,
        path_allowed=is_path_allowed,
    )
    if denial is not None:
        raise ScriptSandboxError(denial)
    envelope = execution_envelope_for_consumed_action(
        None, leased=False, thread_id=thread_id, grant_policies=grant_policies,
        policies=policies, agent_account=github_agent.script_view(), env=env,
        build_profile=_script_profile(view, socket_dir), build_argv=safehouse_argv,
    )
    if socket_dir is not None and envelope is None:
        raise ScriptSandboxError("authenticated script sandbox is unavailable")
    if envelope is not None and not envelope.profile_required:
        envelope = replace(envelope, argv_options={**envelope.argv_options,
                                                  "extra_hidden_roots": _script_gh_homes(view),
                                                  "extra_socket_roots": [socket_dir] if socket_dir else []})
    profile_path, argv = await prepare_execution_envelope(
        script_command(script, stdin, client_dir=client_dir), cwd, envelope,
    )
    try:
        owned = await PROCESS_OWNER.spawn(
            argv, thread_id=thread_id, turn_id=f"script:{run_id}", cwd=cwd,
            env=env, timeout_seconds=timeout_seconds, background=False,
        )
    finally:
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)
    if not isinstance(owned, ForegroundProcessResult):
        raise ScriptSandboxError("process owner returned an invalid result")
    await shell_sandbox.recheck_after(owned)
    return ScriptRun(
        owned.returncode, owned.stdout, owned.stderr, bool(owned.timed_out),
        github_lock_reason(view, approved=socket_dir is not None),
    )
