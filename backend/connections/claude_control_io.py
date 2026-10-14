"""Bounded byte-preserving I/O for Claude stream-JSON control sessions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

_REPLY_PURPOSES = {
    "get_usage": "usage",
    "list_models": "catalog",
    "get_context_usage": "context",
}
_LOG_CORRELATION_KEY = secrets.token_bytes(16)


def log_operation(operation: str) -> str:
    """Return a fixed category instead of a caller-provided operation string."""
    return {"get_usage": "usage", "list_models": "catalog"}.get(operation, "other")


def log_scope(context) -> str:
    """Correlate one account generation without exposing its identifier or home."""
    receipt = None if context is None else context.receipt
    if receipt is None:
        return "default"
    subject = f"{receipt.account_id}:{receipt.account_revision}:{receipt.login_seq}".encode()
    return "scoped-" + hashlib.blake2s(
        subject, key=_LOG_CORRELATION_KEY, digest_size=8,
    ).hexdigest()


@dataclass
class ControlPhase:
    """Local lifecycle facts retained only until a timeout warning is emitted."""

    stdin_written: bool = False
    replies_observed: dict[str, bool] = field(
        default_factory=lambda: {"usage": False, "catalog": False, "context": False},
    )
    process_exited_at_timeout: bool | None = None
    returncode_at_timeout: int | None = None

    def snapshot_timeout(self, process) -> None:
        """Capture process state before the caller's cleanup context can change it."""
        self.process_exited_at_timeout = process.returncode is not None
        self.returncode_at_timeout = process.returncode


def prepare_control_request(frames: list[dict[str, Any]]) -> tuple[bytes, dict[str, str]]:
    """Serialize frames and map only reply-bearing request IDs to fixed purposes."""
    expected: dict[str, str] = {}
    for frame in frames:
        request = frame.get("request")
        request_id = frame.get("request_id")
        subtype = request.get("subtype") if isinstance(request, dict) else None
        purpose = _REPLY_PURPOSES.get(subtype) if isinstance(subtype, str) else None
        if purpose and request_id:
            expected[str(request_id)] = purpose
    payload = "".join(json.dumps(frame) + "\n" for frame in frames).encode()
    return payload, expected


def observe_control_reply(data: bytes, expected: dict[str, str], phase: ControlPhase) -> None:
    """Record expected response presence without retaining payload or disrupting bytes."""
    try:
        response = json.loads(data.decode("utf-8")).get("response") or {}
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return
    if isinstance(response, dict) and (purpose := expected.get(str(response.get("request_id") or ""))):
        phase.replies_observed[purpose] = True


async def _feed_stdin(process, payload: bytes, phase: ControlPhase) -> None:
    """Preserve asyncio communicate's ignored broken-pipe behavior."""
    try:
        process.stdin.write(payload)
        phase.stdin_written = True
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    process.stdin.close()


async def _read_stdout(process, expected: dict[str, str], phase: ControlPhase) -> bytes:
    """Drain stdout incrementally while returning every byte unchanged."""
    chunks: list[bytes] = []
    pending = b""
    while chunk := await process.stdout.read(64 * 1024):
        chunks.append(chunk)
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        for line in lines:
            observe_control_reply(line, expected, phase)
    if pending:
        observe_control_reply(pending, expected, phase)
    process._transport.get_pipe_transport(1).close()
    return b"".join(chunks)


async def communicate(process, payload: bytes, expected: dict[str, str], phase: ControlPhase) -> tuple[bytes, bytes]:
    """Match communicate's concurrent drains and wait while exposing local phase facts."""
    _stdin, stdout, stderr = await asyncio.gather(
        _feed_stdin(process, payload, phase),
        _read_stdout(process, expected, phase),
        process._read_stream(2),
    )
    await process.wait()
    return stdout, stderr
