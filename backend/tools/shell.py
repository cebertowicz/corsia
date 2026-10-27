"""Shell execution tool — Bash-style with description, per-command timeout, background support."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import partial
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from host_access.processes import (
    BackgroundProcessResult,
    ForegroundProcessResult,
    PROCESS_OWNER,
)
from connections import github_agent
from infra.db_async import run_in_db, run_off_db
from infra.project_repo_policy import (
    RepoPolicy,
    RepoPolicyQueryError,
    list_registered_repo_policies,
)
from infra.repo_flock import fast_forward_repo_locked
from tools.shared_write_detection import collect_shell_shared_write_notices
from registry.observer import (
    CommandArtifactRegistrationError,
    observe_command_result,
)
from registry.registration_writer import origin_from_tool_context
from registry.service import notify_changed
import tools.agent_shell_tools as agent_shell_tools, tools.shell_long_running as shell_long_running
from tools.shell_fast_forward import settle_cancelled_fast_forward
from tools.base import Tool
from tools.sandbox_config import (
    check_repo_write_path,
    is_path_allowed,
)
from tools.shell_cwd import (
    _cwd_within_registered_repo,
    _resolve_execution_cwd,
    _thread_has_live_lease,
)
from tools.shell_envelope import (
    execution_envelope_for_consumed_action,
    prepare_execution_envelope,
)
from tools.shell_environment import (
    bind_agent_account,
    shell_execution_context,
    shell_identity_refusal,
)
from tools.shell_output import (
    REGISTRATION_WARNING_RESULT_CAP as BASH_REGISTRATION_WARNING_RESULT_CAP,
    append_denial_guidance,
    foreground_output_parts,
    registration_warning as build_registration_warning,
    with_shell_warnings,
)
from tools.shell_policy import load_shell_repo_policies
import tools.controlled_git_sandbox as controlled_git_sandbox
import tools.shell_sandbox as shell_sandbox
from tools.shell_guard import (
    chained_base_git_denial_guidance,
    check_command_safety,
    check_worktree_targets,
    controlled_fast_forward_policy,
    execution_envelope_profile,
    is_safehouse_available,
    is_sandbox_denial,
    safehouse_argv,
    safehouse_version_error,
)

log = logging.getLogger(__name__)



async def _run_controlled_git(
    repo: Path,
    args: list[str],
    operation: str,
    timeout: float,
    *,
    policy: RepoPolicy,
    env: dict[str, str],
    thread_id: str,
    turn_id: str,
    lease_id: str | None,
    lease_revision: int | None,
    permit_id: str | None,
    agent_account: github_agent.AgentAccountView | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one trusted direct Git argv inside a revalidated Safehouse profile."""
    agent_account = await run_in_db(github_agent.view) if agent_account is None else agent_account
    kind = args[0] if args and args[0] in {"fetch", "merge"} else "query"
    live_policies = list_registered_repo_policies()
    profile_path, argv = await asyncio.to_thread(
        controlled_git_sandbox.envelope, repo, args, policy=policy,
        operation=kind, policies=live_policies, agent_account=agent_account,
        env=env, thread_id=thread_id, build_profile=execution_envelope_profile,
        build_argv=safehouse_argv,
    )
    try:
        owned = await PROCESS_OWNER.spawn(
            argv,
            thread_id=thread_id,
            turn_id=turn_id,
            lease_id=lease_id,
            lease_revision=lease_revision,
            permit_id=permit_id,
            cwd=repo,
            env=env,
            timeout_seconds=timeout,
            background=False,
        )
        if not isinstance(owned, ForegroundProcessResult):
            raise RuntimeError(
                f"{operation} process owner returned an invalid result"
            )
        if owned.timed_out:
            raise subprocess.TimeoutExpired(
                argv,
                timeout,
                output=owned.stdout.decode("utf-8", errors="replace"),
                stderr=owned.stderr.decode("utf-8", errors="replace"),
            )
        return subprocess.CompletedProcess(
            argv,
            owned.returncode,
            owned.stdout.decode("utf-8", errors="replace"),
            owned.stderr.decode("utf-8", errors="replace"),
        )
    finally:
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)


class BashTool(Tool):
    """Execute shell commands with auditability and background support."""

    def __init__(
        self,
        default_timeout: int = 120,
        working_dir: str | None = None,
    ):
        self.default_timeout = default_timeout
        self.working_dir = working_dir

    @property
    def name(self) -> str:
        """Return the tool name exposed to the model."""
        return "bash"

    @property
    def description(self) -> str:
        """Return model-facing guidance for when to use the shell tool."""
        return (
            "Execute a shell command and return its output.\n\n"
            "USE THIS for:\n"
            "- Running build commands, tests, and scripts\n"
            "- Git operations (status, log, diff, commit)\n"
            "- Package management (pip, npm, uv; CORSIA supplies Python, Node, uv and gh)\n"
            "- System commands that don't have a dedicated tool\n\n"
            "DO NOT use this for operations that have dedicated tools:\n"
            "- Reading files → use read_file (not cat/head/tail)\n"
            "- Searching file contents → use grep (not grep/rg command)\n"
            "- Finding files by name → use glob (not find/ls)\n"
            "- Editing files → use edit_file (not sed/awk)\n"
            "- Writing files → use write_file (not echo/cat heredoc)\n"
            "- Listing directories → use list_dir (not ls)\n"
            "- Web pages → use browser_* (not agent-browser)\n\nDedicated tools give better output and are easier to review.\n\n"
            "Features:\n"
            "- Provide a description of what the command does (for auditability)\n"
            "- Set timeout per command (default 120s, max 600s)\n"
            "- Output truncated at 10,000 characters\n"
            "- Protected-mode guards remain active unless an exact reviewed "
            "host-access permit is consumed\n\n"
            "host (boolean, default false): run with reviewed host scope under "
            "your active lease. Use only when protected scope cannot reach the "
            "goal — protected calls are faster and unreviewed. Host calls are "
            "independently reviewed per action.\n\n"
            + shell_long_running.DESCRIPTION_LINE
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Return the JSON schema for shell tool arguments."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of what this command does (e.g., 'Run unit tests', 'Check git status')"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120, max 600)",
                    "minimum": 1,
                    "maximum": 600
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command. Defaults to workspace."
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run as a managed background process and return an "
                        "opaque handle (default: false)"
                    ),
                },
                "host": {
                    "type": "boolean",
                    "description": (
                        "Run with reviewed host scope under your active lease "
                        "(default: false). Use only when protected scope "
                        "cannot reach the goal — protected calls are faster "
                        "and unreviewed. Host calls are independently reviewed "
                        "per action."
                    ),
                },
                **shell_long_running.PROPERTIES,
            },
            "required": ["command"]
        }

    async def execute(
        self,
        command: str,
        description: str | None = None,
        timeout: int | None = None,
        working_dir: str | None = None,
        background: bool = False,
        host: bool = False,
        long_running: bool = False,
        stall_after_seconds: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Run a shell command and return its captured output or guard error."""
        cwd = working_dir or self.working_dir or os.getcwd()
        effective_timeout = min(timeout or self.default_timeout, 600)

        thread_id, project_id, authoritative_turn_id, env = shell_execution_context(
            kwargs, export_invocation=not (background or long_running),
        )
        long_call = shell_long_running.LongRunningCall.from_shell_call(locals())
        action_params = long_call.action_params

        if refusal := shell_identity_refusal(thread_id, authoritative_turn_id):
            return refusal
        if long_call.refusal:
            return long_call.refusal
        agent_account = await bind_agent_account(env)
        profile_path: Path | None = None
        cwd_fd: int | None = None

        try:
            from tools.host_access_gate import (
                consume_current_action_permit,
                protected_scope_denial,
            )
            from tools.host_path_policy import denial_guidance_prefix

            consumed = await consume_current_action_permit(
                thread_id=thread_id,
                project_id=project_id,
                tool=self,
                tool_name=self.name,
                params=action_params,
            )
            if consumed is not None:
                cwd_fd = consumed._working_directory_fd
                if consumed.working_directory is None:
                    return "Error: reviewed shell working directory is unavailable"
                if cwd_fd is None:
                    return "Error: reviewed shell working directory binding is unavailable"
                cwd = str(consumed.working_directory)
                effective_timeout = int(
                    consumed.effective_timeout or effective_timeout
                )
            elif host and await _thread_has_live_lease(thread_id):
                # host:true is the only bash call reviewed under a lease, so a
                # host:true call reaching execution with no permit means the
                # review/permit path was bypassed or lost. Refuse loudly rather
                # than silently running host-scoped work in the narrower
                # protected sandbox. A default (host:false) call under a lease
                # is meant to run protected here, so it is never refused.
                return (
                    "Error: this thread has access to an active host-access lease "
                    "and this bash call requested host scope, but no reviewed "
                    "permit reached execution, so the command was refused "
                    "rather than silently running in protected scope. Retry "
                    "to trigger a fresh action review."
                )

            leased = consumed is not None
            try:
                policies, grant_policies = await asyncio.to_thread(
                    load_shell_repo_policies, list_registered_repo_policies, leased, project_id
                )
            except RepoPolicyQueryError as exc:
                return (
                    "Error: repository policy enforcement is unavailable: "
                    f"{exc}"
                )
            if not leased:
                fast_forward_policy = controlled_fast_forward_policy(
                    command,
                    cwd,
                    policies=policies,
                )
                if fast_forward_policy is not None:
                    if error := await shell_sandbox.startup_error(is_safehouse_available, safehouse_version_error):
                        return error

                    try:
                        owner_thread_id = thread_id
                        owner_turn_id = authoritative_turn_id
                        loop = asyncio.get_running_loop()

                        def _runner(repo, args, operation, remaining):
                            future = asyncio.run_coroutine_threadsafe(
                                _run_controlled_git(
                                    repo,
                                    args,
                                    operation,
                                    remaining,
                                    policy=fast_forward_policy,
                                    env=env,
                                    thread_id=owner_thread_id,
                                    turn_id=owner_turn_id,
                                    lease_id=None,
                                    lease_revision=None,
                                    permit_id=None,
                                    agent_account=agent_account,
                                ),
                                loop,
                            )
                            try:
                                return future.result(timeout=remaining + 10.0)
                            except FutureTimeoutError as exc:
                                future.cancel()
                                raise subprocess.TimeoutExpired(
                                    args,
                                    remaining,
                                ) from exc

                        work = loop.run_in_executor(
                            None,
                            partial(
                                fast_forward_repo_locked,
                                fast_forward_policy,
                                runner=_runner,
                                policy_loader=list_registered_repo_policies,
                                timeout=effective_timeout,
                            ),
                        )
                        try:
                            result = await asyncio.shield(work)
                        except asyncio.CancelledError as cancellation:
                            await settle_cancelled_fast_forward(
                                work,
                                thread_id=owner_thread_id,
                                turn_id=owner_turn_id,
                                cancellation=cancellation,
                            )
                    except Exception as exc:
                        return f"Error: controlled fast-forward failed: {exc}"
                    if result.updated:
                        return (
                            "Fast-forwarded repository "
                            f"'{fast_forward_policy.repo_name}' on branch "
                            f"'{result.branch}' from "
                            f"{result.previous_oid[:12]} to "
                            f"{result.current_oid[:12]}."
                        )
                    return (
                        f"Repository '{fast_forward_policy.repo_name}' is already "
                        f"up to date on branch '{result.branch}' at "
                        f"{result.current_oid[:12]}."
                    )

                guard_error = await asyncio.to_thread(self._guard_command, command, cwd)
                if guard_error:
                    return guard_error

            if error := await shell_sandbox.startup_error(
                is_safehouse_available, safehouse_version_error, leased=leased,
            ):
                return error

            canonical_cwd, cwd_denial = await _resolve_execution_cwd(
                cwd,
                working_dir,
                leased=leased,
                policies=policies,
                thread_id=thread_id, path_allowed=is_path_allowed,
            )
            if cwd_denial is not None:
                return cwd_denial
            await agent_shell_tools.wait_for_prefetch()
            envelope = execution_envelope_for_consumed_action(
                consumed,
                leased=leased, thread_id=thread_id,
                grant_policies=grant_policies,
                policies=policies,
                agent_account=agent_account,
                env=env,
                build_profile=execution_envelope_profile,
                build_argv=safehouse_argv,
            )
            profile_path, argv = await prepare_execution_envelope(
                command, canonical_cwd, envelope,
            )
            shared_write_started_at_ns = time.time_ns()
            owned = await PROCESS_OWNER.spawn(
                argv,
                thread_id=thread_id,
                turn_id=authoritative_turn_id,
                lease_id=getattr(consumed, "lease_id", None),
                lease_revision=getattr(consumed, "lease_revision", None),
                permit_id=getattr(consumed, "permit_id", None),
                cwd=canonical_cwd,
                cwd_fd=cwd_fd,
                env=env,
                timeout_seconds=effective_timeout,
                background=background,
                cleanup_paths=(profile_path,) if background and profile_path is not None else (),
                **long_call.spawn_options,
            )
            if isinstance(owned, BackgroundProcessResult):
                # ProcessOwner now owns the profile through terminal cleanup.
                profile_path = None
                return long_call.started_message(owned.handle)
            if not isinstance(owned, ForegroundProcessResult):
                raise RuntimeError("process owner returned an invalid result")
            if owned.timed_out:
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts, stdout_text, stderr_text = foreground_output_parts(owned)
            combined_output = stdout_text + stderr_text
            if owned.returncode != 0 and not leased and is_sandbox_denial(
                combined_output,
                lambda: shell_sandbox.available_for_denial(is_safehouse_available),
            ):
                # A repo-policy denial (pinned/protected base) has a concrete
                # better non-host path — the exact `.worktrees/` recovery — so
                # that guidance leads. A chained direct-Git form on a base
                # checkout also gets the one-command teaching, because worktrees
                # now grant Git by location and unchaining is the real fix there.
                # The generic state-aware host-scope template applies only when
                # no policy-specific better way exists; never steer to host scope
                # to route around repo policy.
                linux = shell_sandbox.sys.platform == 'linux'
                if linux:
                    output_parts.append('\nIf the sandbox restricted the path, this guidance may help:')
                repo_guidance = check_repo_write_path(canonical_cwd)
                chained_guidance = chained_base_git_denial_guidance(command, str(canonical_cwd))
                append_denial_guidance(output_parts, thread_id, repo_guidance, chained_guidance)
                if not repo_guidance and not chained_guidance:
                    output_parts.append(
                        "\n"
                        + denial_guidance_prefix(combined_output)
                        + protected_scope_denial(
                            ("the sandbox may restrict access to the path" if linux else
                             "this command was blocked by the protected-mode sandbox"),
                            lease_active=await _thread_has_live_lease(thread_id),
                            conditional=linux,
                        )
                    )

            result = "\n".join(output_parts) if output_parts else "(no output)"
            registration_warning = None
            try:
                artifact = await run_in_db(
                    observe_command_result,
                    thread_id,
                    "bash", {"command": command},
                    "\n".join(part for part in (stdout_text, stderr_text) if part),
                    is_error=owned.returncode != 0, notify=False,
                    origin=origin_from_tool_context(thread_id, kwargs, "command_trigger"),
                )
                if artifact is not None:
                    notify_changed(artifact)
            except CommandArtifactRegistrationError as exc:
                log.exception(
                    "connector command artifact registration failed in %s",
                    thread_id,
                )
                registration_warning = build_registration_warning(exc.artifact, exc.error)
            except Exception as exc:
                log.exception(
                    "connector command observation failed in %s",
                    thread_id,
                )
                registration_warning = build_registration_warning(None, exc)

            ownership_notices = await collect_shell_shared_write_notices(
                thread_id, project_id, command, shared_write_started_at_ns, canonical_cwd, runner=run_off_db,
            )
            result = with_shell_warnings(
                result, registration_warning, ownership_notices,
            )

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"
        finally:
            if cwd_fd is not None:
                try:
                    os.close(cwd_fd)
                except OSError:
                    pass
            if profile_path is not None:
                profile_path.unlink(missing_ok=True)

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Apply protected-mode terminal and literal-Git repository guards."""
        cmd = command.strip()
        return check_command_safety(cmd) or check_worktree_targets(cmd, cwd)
