"""Claude Code fork output, including explicit limit and provider failure results."""

from __future__ import annotations

import json
from typing import Any

from core.checkpoint_types import CHECKPOINT_CONTEXT_TRUNCATED, ProviderCheckpointRejected, ProviderCheckpointResult
from core.fork_runtime import ForkStepLimitReached
from infra.provider_usage import TokenUsage, checkpoint_receipt, from_anthropic
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
        return ""  # Context errors are never checkpoint narrative.
    terminal = record.get("result")
    return terminal if not has_body and isinstance(terminal, str) else ""


def result(stdout: bytes, *, truncated: bool = False, kind_name: str = "checkpoint") -> ProviderCheckpointResult:
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
                raise ForkStepLimitReached(f"{kind_name} reached its Claude Code turn limit")
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else usage
            if record.get("is_error") is True:
                terminal = record.get("result")
                diagnostic = str(terminal) if terminal is not None else "result error"
                truncated = truncated or is_provider_context_limit(record)
                if kind_name != "checkpoint" and not truncated:
                    raise ProviderCheckpointRejected(f"{kind_name}: {diagnostic}")
    if not (body := "\n".join(bodies)).strip():
        detail = f"{CHECKPOINT_CONTEXT_TRUNCATED}: {diagnostic or 'no narrative'}" if truncated else None
        raise ProviderCheckpointRejected(detail or f"Claude Code {kind_name} produced no body")
    measured = from_anthropic(usage)
    if measured is None and calls:
        measured = sum(calls.values(), TokenUsage())
    return ProviderCheckpointResult(body, checkpoint_receipt(measured), CHECKPOINT_CONTEXT_TRUNCATED if truncated else diagnostic)


def process_diagnostic(stderr: bytes | str, returncode: int | None, *, kind_name: str = "checkpoint") -> str | None:
    detail = stderr.decode("utf-8", "replace").strip() if isinstance(stderr, bytes) else str(stderr or "").strip()
    if detail or returncode in {None, 0}:
        return detail or None
    return f"Claude Code {kind_name} process exited with code {returncode}"


def result_after_process(stdout: bytes, diagnostic: str | None, returncode: int | None, *, kind_name: str = "checkpoint") -> ProviderCheckpointResult:
    truncated = bool(diagnostic and returncode not in {None, 0} and is_provider_context_limit(diagnostic))
    try:
        parsed = result(stdout, truncated=truncated, kind_name=kind_name)
    except ProviderCheckpointRejected as error:
        if diagnostic and str(error) == f"Claude Code {kind_name} produced no body":
            raise ProviderCheckpointRejected(diagnostic) from error
        raise
    if parsed.diagnostic == CHECKPOINT_CONTEXT_TRUNCATED or not diagnostic or diagnostic == parsed.diagnostic:
        return parsed
    combined = f"{parsed.diagnostic}; {diagnostic}" if parsed.diagnostic else diagnostic
    return ProviderCheckpointResult(parsed.raw_body, parsed.usage, combined)
