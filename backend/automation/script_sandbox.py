"""Run one scheduled script inside the same envelope as its owner's shell.

The shell builds a Safehouse profile and argv for each command, then spawns it
through ``PROCESS_OWNER`` under the thread and Turn identity. A script run has
no Turn, so it uses a synthetic one (``script:<run_id>``) and the unleased
envelope: it can never inherit a host-access lease. Every builder below is the
one ``BashTool.execute`` calls, so the two cannot drift apart.

A rule-started run whose stored event names a repository also gets a GitHub
read channel for that repository (``script_github_channel``) and the
``corsia github read`` client. An approved authenticated run instead gets the
``corsia`` client, the owner tool channel and a ``gh`` forwarder, so it acts
with its agent's access; every other run gets none of these. The forwarder adds
the agents' account's credential, or, when there is no agents' account, the host
``gh`` login's, the login agent shells then use too. When its approval names saved
items, it also gets the saved-item sockets (``script_clients``).

Each run hides the person's and agents' GitHub homes from the script itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from automation import script_clients
from automation.script_clients import ScriptSandboxError
from automation.script_github_channel import SOCKET_ENV as GITHUB_READ_SOCKET_ENV
from automation.script_items import RunItems
from automation.script_tool_channel import SOCKET_ENV
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
from tools.sandbox_git import sandbox_any_case, sandbox_literal
import tools.shell_sandbox as shell_sandbox
from tools.unix_socket_policy import corsia_temp_root_for_bind

log = logging.getLogger(__name__)

#: Stdout the runner will read. One extra byte is kept so a longer stream is seen.
STDOUT_CAP_BYTES = 64 * 1024
STDERR_CAP_BYTES = 8 * 1024
_INTERPRETERS = {
    ".py": "python3", ".sh": "bash", ".bash": "bash", ".js": "node", ".mjs": "node",
}


@dataclass(frozen=True)
class ScriptRun:
    """What one finished run produced."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    #: Why the run had no GitHub login, in plain words; None when it ran as the agents' account.
    github_locked: str | None = None
    #: True when CORSIA's own shutdown stopped the script before it finished.
    stopped_by_shutdown: bool = False
    #: Whether stdout ran past the cap, taken before saved values are blanked out of it; None reads it now.
    over_cap_raw: bool | None = None

    @property
    def over_cap(self) -> bool:
        if self.over_cap_raw is not None:
            return self.over_cap_raw
        return len(self.stdout) > STDOUT_CAP_BYTES


def script_command(path: Path, stdin: Path | None = None, *, client_dir: Path | None = None,
                   pycache_dir: Path | None = None) -> str:
    """The one bash command line for a script: ``stdin`` or none, capped stdout and stderr.

    ``head`` ends a runaway script with SIGPIPE instead of letting the process
    owner hold its whole output in memory, and ``pipefail`` keeps the script's
    own exit code visible. ``pycache_dir`` keeps the run's own Python bytecode out
    of the script's folder; on Linux, which has no ``_folder_bytecode_deny`` yet, it
    is what keeps the main process off a cache planted there.
    """
    quoted = shlex.quote(str(path))
    interpreter = _INTERPRETERS.get(path.suffix)
    run = f"{interpreter} {quoted}" if interpreter else quoted
    source = shlex.quote(str(stdin)) if stdin is not None else "/dev/null"
    client_path = f'export PATH={shlex.quote(str(client_dir))}:"$PATH"; ' if client_dir else ""
    pycache = f"export PYTHONPYCACHEPREFIX={shlex.quote(str(pycache_dir))}; " if pycache_dir else ""
    return (
        f"{client_path}{pycache}set -o pipefail; {run} <{source} "
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
    saved_items: RunItems | None = None,
) -> ScriptRun:
    """Run ``script`` in the owner's protected envelope and return its capped output.

    ``stdin`` is written to a private file in the thread's own temp root, which
    the envelope grants read-write, and removed once the run ends. ``trace`` is a
    rule-started run's rule chain, handed to ``corsia event emit`` (``events.trace_env``).
    ``github_repo`` is the repository a rule run's claimed event names; only then
    does the run get a GitHub read channel, and only for that repository. ``saved_items``
    are what an approved run may use; only an authenticated run gets their sockets.
    """
    await _checked_environment()
    if stdin is None:
        return await _run(thread_id, project_id, script, run_id, timeout_seconds, None, trace,
                          rule_id, github_repo, authenticated, saved_items)
    try:
        fd, name = tempfile.mkstemp(prefix="rule-input-", suffix=".json",
                                    dir=corsia_temp_root_for_bind(thread_id))
    except (OSError, RuntimeError) as exc:
        raise ScriptSandboxError(f"the event input could not be staged: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(stdin)
        return await _run(thread_id, project_id, script, run_id, timeout_seconds, Path(name), trace,
                          rule_id, github_repo, authenticated, saved_items)
    finally:
        Path(name).unlink(missing_ok=True)


async def _run(
    thread_id: str, project_id: str | None, script: Path, run_id: str, timeout_seconds: int,
    stdin: Path | None, trace: tuple[str, ...], rule_id: str | None, github_repo: str | None,
    authenticated: bool, saved_items: RunItems | None = None,
) -> ScriptRun:
    view = await run_in_db(github_agent.view)
    if view.answer_pending:
        view = await run_off_db(github_agent.verify_now)
    if not authenticated:
        if github_repo is not None:
            async with script_clients._legacy_github_client(thread_id, run_id, rule_id, github_repo) as (
                socket, client,
            ):
                return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                               stdin, trace, None, None, None, client, view,
                                               github_read_socket=socket)
        return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                       stdin, trace, None, None, None, None, view)
    async with script_clients._authenticated_client(thread_id, view, saved_items) as (
        socket_dir, tool_socket, config_dir, client_dir, item_env,
    ):
        return await _run_with_channel(thread_id, project_id, script, run_id, timeout_seconds,
                                       stdin, trace, socket_dir, tool_socket, config_dir, client_dir, view,
                                       item_env=item_env)


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
_UNAPPROVED = ("scripts get no GitHub login: this script isn't approved to use GitHub; "
               "only if it failed on a GitHub call that needs a login, schedule it with "
               "authenticated=true so the person can approve it")
_NO_HOST_LOGIN = ("there is no agents' GitHub account and no GitHub CLI login on this computer; the person "
                  "needs to sign in to GitHub CLI or connect an agents' GitHub account")


def github_lock_reason(view: AgentAccountView, *, approved: bool = True, host_login: bool = True) -> str | None:
    """Why a run gets no GitHub login, or None: approved runs read as a verified agents' account
    or, with none, as the host ``gh`` login when ``host_login`` says it exists."""
    if view.ready or view.absent:
        if not approved:
            return _UNAPPROVED
        return None if view.ready or host_login else f"scripts get no GitHub login: {_NO_HOST_LOGIN}"
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


def _folder_bytecode_deny(folder: Path) -> str:
    """Refuse every ``__pycache__`` under an authenticated script's folder.

    The approval leaves bytecode caches out (``script_tree``), so a cache planted after
    approval would run unreviewed as the agents' account. A refused read makes Python
    compile the reviewed source instead, whatever flags or environment a process in the
    run starts with.
    """
    cache = sandbox_any_case(Path("__pycache__"))
    return f'(deny file-read* file-write* (regex #"^{sandbox_any_case(folder.resolve())}/(.*/)?{cache}(/.*)?$"))\n'


def _script_profile(view: AgentAccountView, socket_dir: Path | None = None, folder: Path | None = None):
    hidden = _script_gh_homes(view)

    def build(*args, **kwargs) -> str:
        profile = execution_envelope_profile(*args, **kwargs)
        for path in hidden:
            profile += f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))\n'
        if socket_dir is not None:
            root = sandbox_literal(socket_dir.resolve())
            profile += f'(allow file-read* (subpath "{root}"))\n'
            profile += f'(allow network-outbound (remote unix-socket (subpath "{root}")))\n'
            if folder is not None:
                profile += _folder_bytecode_deny(folder)
        return profile
    return build


async def _run_with_channel(
    thread_id: str, project_id: str | None, script: Path, run_id: str, timeout_seconds: int,
    stdin: Path | None, trace: tuple[str, ...], socket_dir: Path | None, socket_path: Path | None,
    config_dir: Path | None, client_dir: Path | None, view: AgentAccountView,
    *, github_read_socket: Path | None = None, item_env: dict[str, str] | None = None,
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
    env.update(item_env or {})
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
        build_profile=_script_profile(view, socket_dir, script.parent), build_argv=safehouse_argv,
    )
    if socket_dir is not None and envelope is None:
        raise ScriptSandboxError("authenticated script sandbox is unavailable")
    if envelope is not None and not envelope.profile_required:
        envelope = replace(envelope, argv_options={**envelope.argv_options,
                                                  "extra_hidden_roots": _script_gh_homes(view),
                                                  "extra_socket_roots": [socket_dir] if socket_dir else []})
    profile_path, argv = await prepare_execution_envelope(
        script_command(script, stdin, client_dir=client_dir,
                       pycache_dir=client_dir / "pycache" if socket_dir is not None and client_dir else None),
        cwd, envelope,
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
    locked = github_lock_reason(view, approved=socket_dir is not None, host_login=config_dir is not None)
    if locked is not None and socket_dir is not None:
        log.warning("approved script ran with no GitHub login thread=%s run=%s: %s", thread_id, run_id, locked)
    return ScriptRun(
        owned.returncode, owned.stdout, owned.stderr, bool(owned.timed_out),
        locked,
        # A script that ended with 0 finished its work, whatever the owner did after it.
        stopped_by_shutdown=owned.stop_reason == "backend_shutdown" and owned.returncode != 0,
    )
