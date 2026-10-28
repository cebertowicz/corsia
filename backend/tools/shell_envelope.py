"""Write the protected shell execution envelope off the server event loop.

Building the sandbox profile and the Safehouse argv reads repository policy,
which queries SQLite and runs Git for every registered repository, so the work
runs in a worker thread. The profile and argv builders are injected so the
caller's bindings are the ones consulted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tools.private_runtime_files import SHELL_ENVELOPE_DIR, private_mkstemp


@dataclass(frozen=True)
class Envelope:
    """The injected builders for one envelope and the options each receives."""

    build_profile: Callable[..., str]
    build_argv: Callable[..., list[str]]
    profile_options: dict[str, Any] = field(default_factory=dict)
    argv_options: dict[str, Any] = field(default_factory=dict)
    profile_required: bool = True


log = logging.getLogger(__name__)


def _write_envelope(command: str, cwd: Path, envelope: Envelope) -> tuple[Path | None, list[str]]:
    if not envelope.profile_required:
        return None, envelope.build_argv(command, cwd, None, **envelope.argv_options)
    descriptor, raw_path = private_mkstemp(
        SHELL_ENVELOPE_DIR, prefix="corsia-shell-envelope-", suffix=".sb",
    )
    profile_path = Path(raw_path).resolve(strict=False)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(envelope.build_profile(
                command, cwd, profile_path, **envelope.profile_options,
            ))
        argv = envelope.build_argv(command, cwd, profile_path, **envelope.argv_options)
        return profile_path, argv
    except BaseException:
        profile_path.unlink(missing_ok=True)
        raise


def _discard_unclaimed_envelope(work: asyncio.Future) -> None:
    """Remove the profile a cancelled caller will never run or clean up."""
    if work.cancelled():
        return
    if (error := work.exception()) is not None:
        # The worker already removed its profile; nobody awaits this failure.
        log.warning(
            "shell envelope preparation failed after its caller was cancelled",
            exc_info=error,
        )
        return
    profile_path, _argv = work.result()
    if profile_path is not None:
        profile_path.unlink(missing_ok=True)


def execution_envelope_for_consumed_action(
    consumed: Any,
    *,
    leased: bool,
    thread_id: str,
    grant_policies: list[Any],
    policies: list[Any],
    agent_account: Any,
    env: dict[str, str],
    build_profile: Callable[..., str],
    build_argv: Callable[..., list[str]],
) -> Envelope:
    """Build the one envelope, applying external child confinement if trusted."""
    from tools.shell_environment import strip_external_browser_environment

    external_agent = bool(getattr(consumed, "external_agent", False))
    if external_agent:
        strip_external_browser_environment(env)
    if sys.platform == 'linux' and not leased:
        from tools.linux_shell import shell_argv
        return Envelope(
            build_profile, shell_argv, profile_required=False,
            argv_options={'thread_id': thread_id, 'policies': policies,
                          'env': env, 'agent_account': agent_account},
        )
    return Envelope(
        build_profile,
        build_argv,
        profile_options={
            "leased": leased,
            "external_agent": external_agent,
            "grant_policies": grant_policies,
            "policies": policies,
            "agent_account": agent_account,
            "thread_id": thread_id,
        },
        argv_options={
            "leased": leased,
            "external_agent": external_agent,
            "agent_account": agent_account,
            "thread_id": thread_id,
            "policies": policies,
        },
    )


async def prepare_execution_envelope(
    command: str,
    cwd: Path,
    envelope: Envelope,
) -> tuple[Path | None, list[str]]:
    """Write a private sandbox profile and return it with the Safehouse argv.

    The caller owns the returned profile path. If the caller is cancelled before
    the worker finishes, the worker's profile is removed when it does.
    """
    work = asyncio.ensure_future(asyncio.to_thread(_write_envelope, command, cwd, envelope))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        work.add_done_callback(_discard_unclaimed_envelope)
        raise
