"""Claude Code fork output, including explicit limit and provider failure results."""

from __future__ import annotations

import json
from typing import Any

from core.fork_types import (
    ProviderForkRejected,
    ProviderForkResult,
)
from core.fork_types import FORK_CONTEXT_TRUNCATED
from core.fork_runtime import ForkKind, ForkStepLimitReached
from infra.provider_usage import TokenUsage, fork_receipt, from_anthropic
from subagents.provider_context_limits import is_provider_context_limit


def _assistant_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()
    )


def _result_body(record: dict[str, Any], *, has_body: bool) -> str:
    if record.get("type") == "assistant":
        return _assistant_text(record)
    if record.get("type") != "result":
        return ""
    if record.get("is_error") is True and is_provider_context_limit(record):
        return ""  # Context errors are never fork output.
    terminal = record.get("result")
    return terminal if not has_body and isinstance(terminal, str) else ""


def _no_body_detail(stdout: bytes) -> str:
    """Say what the process printed instead of a body, without any of its text."""
    counts: dict[str, int] = {}
    blocks: set[str] = set()
    terminal = None
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            counts["unparsed"] = counts.get("unparsed", 0) + 1
            continue
        kind = str(record.get("type")) if isinstance(record, dict) else "unparsed"
        if kind == "stream_event":
            continue
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "result":
            terminal = record
        elif kind == "assistant":
            content = (record.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    blocks.add(str(block.get("type")))
    parts = []
    if terminal is None:
        parts.append("no result record")
    else:
        status = str(terminal.get("subtype") or "record")
        status += " error" if terminal.get("is_error") is True else ""
        text = f"result {status} with empty text"
        if isinstance(terminal.get("num_turns"), int):
            text += f" after {terminal['num_turns']} turns"
        if terminal.get("stop_reason"):
            text += f" (stop {terminal['stop_reason']})"
        parts.append(text)
    if counts.get("assistant"):
        thinking = {"thinking", "redacted_thinking"}
        parts.append("assistant replies had thinking only" if blocks and blocks <= thinking
                     else f"assistant replies had no text ({', '.join(sorted(blocks)) or 'empty'})")
    elif not counts:
        parts.append("no stdout records")
    elif terminal is None:
        parts.append("stdout records: " + ", ".join(f"{name} x{count}" for name, count in counts.items()))
    return "; ".join(parts)


def result(
    stdout: bytes, *, kind: ForkKind, truncated: bool = False, process: str | None = None,
) -> ProviderForkResult:
    """Parse fork stdout into its body; a missing body names what stdout and ``process`` held."""
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
                calls[str(message.get("id") or f"unidentified-{len(calls)}")] = measured
        if record.get("type") == "result":
            if record.get("subtype") == "error_max_turns":
                raise ForkStepLimitReached(f"{kind.name} reached its Claude Code turn limit")
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else usage
            if record.get("is_error") is True:
                terminal = record.get("result")
                diagnostic = str(terminal) if terminal is not None else "result error"
                truncated = truncated or is_provider_context_limit(record)
                if not truncated:
                    kind.provider_failure("claude-code", diagnostic)
    if not (body := "\n".join(bodies)).strip():
        if truncated:
            raise ProviderForkRejected(f"{FORK_CONTEXT_TRUNCATED}: {diagnostic or 'no narrative'}")
        detail = "; ".join(filter(None, (_no_body_detail(stdout), process)))
        raise ProviderForkRejected(f"Claude Code {kind.name} produced no body: {detail}")
    measured = from_anthropic(usage)
    if measured is None and calls:
        measured = sum(calls.values(), TokenUsage())
    return ProviderForkResult(body, fork_receipt(measured), FORK_CONTEXT_TRUNCATED if truncated else diagnostic)


def process_diagnostic(stderr: bytes | str, returncode: int | None, *, kind: ForkKind) -> str | None:
    detail = stderr.decode("utf-8", "replace").strip() if isinstance(stderr, bytes) else str(stderr or "").strip()
    if detail or returncode in {None, 0}:
        return detail or None
    return f"Claude Code {kind.name} process exited with code {returncode}"


def result_after_process(
    stdout: bytes, diagnostic: str | None, returncode: int | None, *, kind: ForkKind,
    input_note: str | None = None,
) -> ProviderForkResult:
    """Parse a finished process's stdout with its diagnostic, exit code and input outcome."""
    truncated = bool(diagnostic and returncode not in {None, 0} and is_provider_context_limit(diagnostic))
    exit_note = f"exit code {returncode}" if returncode is not None else "no exit code"
    generic = diagnostic == process_diagnostic(b"", returncode, kind=kind)
    process = "; ".join(filter(None, (None if generic else diagnostic, exit_note, input_note)))
    parsed = result(stdout, truncated=truncated, kind=kind, process=process)
    if parsed.diagnostic == FORK_CONTEXT_TRUNCATED or not diagnostic or diagnostic == parsed.diagnostic:
        return parsed
    combined = f"{parsed.diagnostic}; {diagnostic}" if parsed.diagnostic else diagnostic
    return ProviderForkResult(parsed.raw_body, parsed.usage, combined)
