"""One disposable Claude Code checkpoint over a frozen transcript prefix."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from history.types import HistoryBoundary
from infra.provider_usage import TokenUsage, checkpoint_receipt, from_anthropic
from llm.resolution import ExecutionTarget
from subagents.cc_invocation import ClaudeInvocation, build_claude_cmd
from subagents.cc_sessions import resolve_claude_dir
from subagents.provider_context_limits import is_provider_context_limit

from core.checkpoint_types import (
    CHECKPOINT_CONTEXT_TRUNCATED,
    ColdInputCallback,
    ProviderCheckpointRejected,
    ProviderCheckpointRequest,
    ProviderCheckpointResult,
)
from core.checkpoint_warm_request import warm_capture_refused


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
    return provider_id, invocation, claude_dir, transcript, getattr(session, 'vendor_account_receipt', None)


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
    return provider_id, invocation, claude_dir, transcript, receipt


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
        identity[4] if len(identity) == 5 else None,
    )


def _checkpoint_env(source: CCWarmSource | None, request: ProviderCheckpointRequest):
    from subagents.cc_env import apply_claude_config_dir, clean_claude_env

    env = clean_claude_env()
    env["CORSIA_THREAD_ID"] = request.thread_id
    env["CORSIA_CHECKPOINT_ID"] = request.checkpoint_id
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
    env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
    if source is None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request.max_output_tokens)
    apply_claude_config_dir(env, request.vendor_launch.claude_dir if request.vendor_launch else (source.claude_dir if source else None))
    return env


async def _spawn_process(argv: list[str], *, cwd: str, env: dict[str, str]):
    from subagents.cc_stream import STDOUT_LIMIT

    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STDOUT_LIMIT,
        env=env,
    )


def _snapshot(source: CCWarmSource) -> tuple[str, Path]:
    path = Path(source.transcript_path)
    with open(path, "rb") as stream:
        prefix = stream.read(source.transcript_bytes)
    if (
        len(prefix) != source.transcript_bytes
        or hashlib.sha256(prefix).hexdigest() != source.transcript_sha256
    ):
        raise RuntimeError("Claude checkpoint transcript prefix changed")
    snapshot_id = str(uuid4())
    snapshot = path.with_name(f"{snapshot_id}.jsonl")
    with open(snapshot, "xb") as stream:
        stream.write(prefix)
    return snapshot_id, snapshot


def _warm_command(
    source: CCWarmSource,
    request: ProviderCheckpointRequest,
    snapshot_id: str,
) -> list[str]:
    invocation = replace(
        source.invocation,
        binary=request.binary or source.invocation.binary,
        resume_session_id=snapshot_id,
    )
    return build_claude_cmd(
        invocation,
        stream_input=True,
        extra_args=["--fork-session", "--no-session-persistence"],
    )


def _cold_command(request: ProviderCheckpointRequest) -> list[str]:
    if not request.binary:
        raise ValueError("Claude Code checkpoint requires its harness binary")
    invocation = ClaudeInvocation(
        binary=request.binary,
        permission_mode="bypassPermissions",
        mcp_config=request.mcp_config,
        working_dir=request.working_dir,
        model=request.target.transport_model,
        effort=request.reasoning_effort,
    )
    return build_claude_cmd(
        invocation, stream_input=True, extra_args=["--no-session-persistence"]
    )


def _assistant_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    )


def _result_body(record: dict[str, Any], *, has_body: bool) -> str:
    if record.get("type") == "assistant":
        return _assistant_text(record)
    if record.get("type") != "result":
        return ""
    terminal = record.get("result")
    if record.get("is_error") is True and is_provider_context_limit(record):
        return ""  # Provider error text is not checkpoint narrative.
    if not has_body and isinstance(terminal, str):
        return terminal
    return ""


def _result(stdout: bytes, *, truncated: bool = False) -> ProviderCheckpointResult:
    bodies: list[str] = []
    usage, diagnostic = None, None
    calls: dict[str, TokenUsage] = {}
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        part = _result_body(record, has_body=bool(bodies))
        if part.strip():
            bodies.append(part)
        if record.get("type") == "assistant":
            message = record.get("message") or {}
            measured = from_anthropic(message.get("usage")) if isinstance(message, dict) else None
            if measured is not None:
                # Repeated completed frames update the same provider call.
                calls[str(message.get("id") or f"unidentified-{len(calls)}")] = measured
        if record.get("type") == "result":
            usage = (
                record.get("usage") if isinstance(record.get("usage"), dict) else usage
            )
            if record.get("is_error") is True:
                terminal = record.get("result")
                diagnostic = str(terminal) if terminal is not None else "result error"
                truncated = truncated or is_provider_context_limit(record)
    if not (body := "\n".join(bodies)).strip():  # Fail named, never as an empty success.
        detail = f"{CHECKPOINT_CONTEXT_TRUNCATED}: {diagnostic or 'no narrative'}" if truncated else None
        raise ProviderCheckpointRejected(detail or "Claude Code checkpoint produced no body")
    measured = from_anthropic(usage)
    if measured is None and calls:
        measured = sum(calls.values(), TokenUsage())
    return ProviderCheckpointResult(
        body, checkpoint_receipt(measured),
        CHECKPOINT_CONTEXT_TRUNCATED if truncated else diagnostic,
    )


def _process_diagnostic(stderr: bytes | str, returncode: int | None) -> str | None:
    detail = (
        stderr.decode("utf-8", "replace").strip()
        if isinstance(stderr, bytes)
        else str(stderr or "").strip()
    )
    if detail or returncode in {None, 0}:
        return detail or None
    return f"Claude Code checkpoint process exited with code {returncode}"


def _result_after_process(
    stdout: bytes,
    diagnostic: str | None,
    returncode: int | None,
) -> ProviderCheckpointResult:
    truncated = bool(diagnostic and returncode not in {None, 0}
                     and is_provider_context_limit(diagnostic))
    try:
        result = _result(stdout, truncated=truncated)
    except ProviderCheckpointRejected as error:
        if diagnostic and str(error) == "Claude Code checkpoint produced no body":
            raise ProviderCheckpointRejected(diagnostic) from error
        raise
    if (result.diagnostic == CHECKPOINT_CONTEXT_TRUNCATED
            or not diagnostic or diagnostic == result.diagnostic):
        return result
    combined = f"{result.diagnostic}; {diagnostic}" if result.diagnostic else diagnostic
    return ProviderCheckpointResult(result.raw_body, result.usage, combined)


async def invoke_checkpoint(
    request: ProviderCheckpointRequest,
    cancel_event: asyncio.Event,
    *,
    on_cold_input: ColdInputCallback | None = None,
    on_tool_executor=None,
) -> ProviderCheckpointResult:
    """Run one CC process; any captured body is authoritative."""
    if request.target.harness != "claude-code":
        raise TypeError("Claude adapter requires a Claude Code target")
    from core.checkpoint_account import admit_checkpoint

    request = admit_checkpoint(request)
    source = request.warm_source
    if source is not None and not isinstance(source, CCWarmSource):
        raise TypeError("Claude checkpoint received another harness's warm source")
    from core.checkpoint_account import require_checkpoint_write
    from connections.account_operation import require_same_operation_home

    require_same_operation_home(request, source.claude_dir if source else None)
    require_checkpoint_write(request)
    snapshot = None
    restored_seed = None
    try:
        if source is None:
            argv = _cold_command(request)
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
            argv = _warm_command(source, request, snapshot_id)
            cwd = source.invocation.working_dir or request.working_dir
        spawned_at = time.monotonic()
        process = await _spawn_process(
            argv, cwd=cwd, env=_checkpoint_env(source, request)
        )
        from core.checkpoint_cc_io import communicate_checkpoint

        output = await communicate_checkpoint(
            process,
            request,
            cancel_event,
            on_cold_input,
            on_tool_executor=on_tool_executor,
            spawned_at=spawned_at,
        )
        if output.truncated:
            return _result(output.stdout, truncated=True)
        diagnostic = output.failure or _process_diagnostic(output.stderr, process.returncode)
        return _result_after_process(output.stdout, diagnostic, process.returncode)
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        if restored_seed is not None:
            restored_seed.unlink(missing_ok=True)
