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
"""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from automation.script_github_channel import SOCKET_ENV, github_channel
from connections.github_agent_home import default_home
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
from tools.shell_environment import safehouse_subprocess_env
from tools.shell_guard import (
    execution_envelope_profile,
    safehouse_argv,
    safehouse_version_error,
)
from tools.shell_policy import load_shell_repo_policies
from tools.sandbox_git import sandbox_literal
import tools.shell_sandbox as shell_sandbox
from tools.unix_socket_policy import corsia_temp_root_for_bind

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
                          rule_id, github_repo)
    try:
        fd, name = tempfile.mkstemp(prefix="rule-input-", suffix=".json",
                                    dir=corsia_temp_root_for_bind(thread_id))
    except (OSError, RuntimeError) as exc:
        raise ScriptSandboxError(f"the event input could not be staged: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(stdin)
        return await _run(thread_id, project_id, script, run_id, timeout_seconds, Path(name), trace,
                          rule_id, github_repo)
    finally:
        Path(name).unlink(missing_ok=True)


async def _run(
    thread_id: str, project_id: str | None, script: Path, run_id: str, timeout_seconds: int,
    stdin: Path | None, trace: tuple[str, ...], rule_id: str | None, github_repo: str | None,
) -> ScriptRun:
    if github_repo is None:
        return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                       stdin, trace, None, None)
    async with _github_client(thread_id, run_id, rule_id, github_repo) as (socket_path, client_dir):
        return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                       stdin, trace, socket_path, client_dir)


@asynccontextmanager
async def _github_client(thread_id: str, run_id: str, rule_id: str | None, repo: str):
    """The run's channel socket and a directory holding its ``corsia`` client.

    Only a failure to set these up is reported as the channel's; the run's own
    failures keep their cause.
    """
    stack = AsyncExitStack()
    try:
        socket_path = await stack.enter_async_context(github_channel(thread_id, run_id, rule_id, repo=repo))
        client_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="github-client-", dir=corsia_temp_root_for_bind(thread_id))))
        client = client_dir / "corsia"
        client.write_bytes((Path(__file__).resolve().parents[1] / "cli_github.py").read_bytes())
        client.chmod(0o700)
    except OSError as exc:
        await stack.aclose()
        raise ScriptSandboxError(f"script GitHub channel unavailable: {type(exc).__name__}: {exc}") from exc
    async with stack:
        yield socket_path, client_dir


def _script_gh_homes() -> set[Path]:
    paths = {default_home(), Path.home() / ".config" / "gh"}
    if os.environ.get("XDG_CONFIG_HOME"):
        paths.add(Path(os.environ["XDG_CONFIG_HOME"]) / "gh")
    return {path.resolve(strict=False) for path in paths}


def _script_profile(*args, **kwargs) -> str:
    profile = execution_envelope_profile(*args, **kwargs)
    for path in _script_gh_homes():
        profile += f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))\n'
    return profile


async def _run_with_channel(
    thread_id: str, project_id: str | None, script: Path, run_id: str, timeout_seconds: int,
    stdin: Path | None, trace: tuple[str, ...], socket_path: Path | None, client_dir: Path | None,
) -> ScriptRun:
    env = safehouse_subprocess_env(thread_id)
    env.pop(SOCKET_ENV, None)
    if socket_path is not None:
        env[SOCKET_ENV] = str(socket_path)
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
        policies=policies, agent_account=None, env=env,
        build_profile=_script_profile, build_argv=safehouse_argv,
    )
    if envelope is not None and not envelope.profile_required:
        envelope = replace(envelope, argv_options={**envelope.argv_options,
                                                  "extra_hidden_roots": _script_gh_homes()})
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
    )
