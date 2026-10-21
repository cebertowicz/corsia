"""One disposable Claude Code fork over a frozen transcript prefix."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from history.types import HistoryBoundary
from llm.resolution import ExecutionTarget
from core import fork_cc_commands
from subagents.cc_invocation import ClaudeInvocation
from subagents.cc_sessions import resolve_claude_dir
from core.fork_runtime import ForkKind
from core.fork_failures import ForkBinaryResolutionError
from core.fork_types import (
    ProviderForkRequest,
    ProviderForkResult,
)
from core.fork_types import ColdInputCallback
from core.fork_capture import warm_capture_refused


@dataclass(frozen=True, slots=True)
class CCWarmSource:
    """A durable Claude transcript prefix and its ordinary launch settings."""

    thread_id: str
    boundary: HistoryBoundary
    provider_session_id: str
    transcript_path: str
    transcript_bytes: int
    transcript_sha256: str
    invocation: ClaudeInvocation
    claude_dir: str | None
    restored_seed_text: str | None = None
    vendor_account_receipt: object | None = None
    #: The live session's tool list, re-advertised so the resumed prefix stays cached.
    tool_definitions: tuple[dict[str, Any], ...] | None = None


def _transcript_path(provider_id: str, claude_dir: str) -> str | None:
    from subagents.cc_sessions import find_cc_session

    transcript = find_cc_session(provider_id, claude_dir=claude_dir)
    path = transcript.get("path") if transcript else None
    return path if isinstance(path, str) and path else None


def _identity_from_live(live: Any):
    session = getattr(live, "session", None)
    if session is None or not hasattr(session, "invocation"):
        return None
    invocation = session.invocation()
    provider_id = invocation.resume_session_id or invocation.session_id
    claude_dir = getattr(live, "claude_dir", None) or getattr(
        session, "claude_dir", None
    )
    # Only a Default-account process carries no root: admission binds every other.
    transcript = _transcript_path(provider_id, claude_dir or resolve_claude_dir()) if provider_id else None
    if transcript is None:
        return None
    definitions = getattr(live, "tool_definitions", None)
    return (provider_id, invocation, claude_dir, transcript, getattr(session, 'vendor_account_receipt', None),
            tuple(definitions) if definitions is not None else None)


async def _live_identity(thread_id: str):
    from subagents.cc_runtime_state import _LIVE

    async with _LIVE.lock(thread_id):
        return _identity_from_live(_LIVE.get(thread_id))


def _registered_identity(thread_id: str):
    from connections.account_runtime import thread_claude_dir
    from infra.database import get_db
    from infra.vendor_login import thread_login
    from subagents.cc_runtime_helpers import (
        _artifact_cc_session_id,
        _artifact_cc_session_invocation,
    )

    db = get_db()
    try:
        provider_id = _artifact_cc_session_id(thread_id, db)
        invocation = _artifact_cc_session_invocation(thread_id, db)
        receipt = thread_login(thread_id, "claude-code", db=db)
        claude_dir = thread_claude_dir(thread_id, db=db)
    finally:
        db.close()
    if not provider_id or not isinstance(invocation, ClaudeInvocation):
        return None
    transcript = _transcript_path(provider_id, claude_dir)
    if transcript is None:
        return None
    return provider_id, invocation, claude_dir, transcript, receipt, None


def _read_prefix(path: Path, prefix_bytes: int | None) -> bytes:
    with path.open("rb") as stream:
        return stream.read(prefix_bytes)


async def capture_warm_source(
    thread_id: str,
    boundary: HistoryBoundary,
    *,
    target: ExecutionTarget,
    binary: str,
    live: Any | None = None,
    prefix_bytes: int | None = None,
) -> CCWarmSource | None:
    """Freeze exactly the transcript prefix present at the safe boundary."""
    if target.harness != "claude-code" or boundary.thread_id != thread_id:
        return warm_capture_refused(thread_id, "claude-code", "target_mismatch")
    identity = _identity_from_live(live) if live is not None else await _live_identity(thread_id)
    if identity is None:
        identity = _registered_identity(thread_id)
    if identity is None:
        return warm_capture_refused(thread_id, "claude-code", "no_identity")
    provider_id, invocation, claude_dir, transcript_path = identity[:4]
    path = Path(transcript_path)
    if prefix_bytes is not None and (
        isinstance(prefix_bytes, bool)
        or not isinstance(prefix_bytes, int)
        or prefix_bytes <= 0
    ):
        return warm_capture_refused(thread_id, "claude-code", "prefix_past_transcript")
    try:
        prefix = await asyncio.to_thread(_read_prefix, path, prefix_bytes)
    except OSError:
        return warm_capture_refused(thread_id, "claude-code", "transcript_unreadable")
    if prefix_bytes is not None and len(prefix) != prefix_bytes:
        return warm_capture_refused(thread_id, "claude-code", "prefix_past_transcript")
    if not prefix:
        return warm_capture_refused(thread_id, "claude-code", "empty_prefix")
    invocation = replace(invocation, binary=binary)
    launch_files = invocation.seed_file, invocation.mcp_config
    if not all(path and Path(path).is_file() for path in launch_files):
        return warm_capture_refused(thread_id, "claude-code", "launch_files_missing")
    restored_seed_text = None
    return CCWarmSource(
        thread_id,
        boundary,
        provider_id,
        str(path),
        len(prefix),
        hashlib.sha256(prefix).hexdigest(),
        invocation,
        claude_dir,
        restored_seed_text,
        *(tuple(identity[4:]) + (None, None))[:2],
    )


def _fork_env(source: CCWarmSource | None, request: ProviderForkRequest):
    from subagents.cc_env import apply_claude_config_dir, clean_claude_env

    env = clean_claude_env()
    env["CORSIA_THREAD_ID"] = request.thread_id
    env["CORSIA_FORK_ID"] = request.fork_id
    from core.fork_runtime import running_fork

    handle = running_fork(request.thread_id, request.fork_id)
    if handle is not None:
        env["CORSIA_FORK_TOOL_TOKEN"] = handle.tool_token
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
    env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
    if source is None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request.max_output_tokens)
    apply_claude_config_dir(env, request.vendor_launch.claude_dir if request.vendor_launch else (source.claude_dir if source else None))
    return env


async def _spawn_process(argv: list[str], *, cwd: str, env: dict[str, str]):
    from subagents.cc_stream import STDOUT_LIMIT

    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT,
            env=env,
        )
    except (FileNotFoundError, PermissionError) as error:
        if error.filename != argv[0]:
            raise
        raise ForkBinaryResolutionError("Claude Code harness executable is unavailable") from error


def _snapshot(source: CCWarmSource) -> tuple[str, Path]:
    path = Path(source.transcript_path)
    with open(path, "rb") as stream:
        prefix = stream.read(source.transcript_bytes)
    if (
        len(prefix) != source.transcript_bytes
        or hashlib.sha256(prefix).hexdigest() != source.transcript_sha256
    ):
        raise RuntimeError("Claude fork transcript prefix changed")
    snapshot_id = str(uuid4())
    snapshot = path.with_name(f"{snapshot_id}.jsonl")
    with open(snapshot, "xb") as stream:
        stream.write(prefix)
    return snapshot_id, snapshot



from core import fork_cc_result


async def invoke_fork(request, cancel_event, *, kind: ForkKind,
                            on_cold_input=None, on_tool_executor=None):
    from core.fork_cc_bridge import bind_bridge

    kind.step_limit("claude-code", request)
    with bind_bridge(request, kind, cancel_event) as (request, handle):
        async def bind_executor(executor):
            handle.tool_executor = executor
            if on_tool_executor is not None:
                await on_tool_executor(executor)

        return await _invoke_fork(
            request, cancel_event, kind=kind, on_cold_input=on_cold_input,
            on_tool_executor=bind_executor,
        )


async def _invoke_fork(
    request: ProviderForkRequest,
    cancel_event: asyncio.Event,
    *,
    kind: ForkKind,
    on_cold_input: ColdInputCallback | None = None,
    on_tool_executor=None,
) -> ProviderForkResult:
    """Run one disposable fork through the Claude Code process."""
    if request.target.harness != "claude-code":
        raise TypeError("Claude adapter requires a Claude Code target")
    from core.fork_account import admit_fork

    request = admit_fork(request)
    source = request.warm_source
    if source is not None and not isinstance(source, CCWarmSource):
        raise TypeError(f"Claude {kind.name} received another harness's warm source")
    from core.fork_account import require_fork_write
    from connections.account_operation import require_same_operation_home

    require_same_operation_home(request, source.claude_dir if source else None)
    require_fork_write(request)
    snapshot = None
    restored_seed = None
    try:
        if source is None:
            argv = fork_cc_commands.cold_command(request, kind)
            cwd = request.working_dir
        else:
            if source.restored_seed_text is not None:
                from subagents.cc_runtime_helpers import _write_seed_file

                restored_seed = Path(_write_seed_file(
                    request.thread_id, source.restored_seed_text,
                ))
                source = replace(
                    source,
                    invocation=replace(source.invocation, seed_file=str(restored_seed)),
                )
            snapshot_id, snapshot = _snapshot(source)
            argv = fork_cc_commands.warm_command(source, request, kind, snapshot_id)
            cwd = source.invocation.working_dir or request.working_dir
        spawned_at = time.monotonic()
        process = await _spawn_process(
            argv, cwd=cwd, env=_fork_env(source, request)
        )
        from core.fork_cc_io import communicate_fork

        output = await communicate_fork(
            process,
            request,
            cancel_event,
            on_cold_input,
            on_tool_executor=on_tool_executor,
            spawned_at=spawned_at,
        )
        if output.truncated:
            return fork_cc_result.result(output.stdout, truncated=True, kind=kind)
        diagnostic = output.failure or fork_cc_result.process_diagnostic(output.stderr, process.returncode, kind=kind)
        return fork_cc_result.result_after_process(output.stdout, diagnostic, process.returncode, kind=kind)
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        if restored_seed is not None:
            restored_seed.unlink(missing_ok=True)
