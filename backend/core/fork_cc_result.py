"""Claude Code fork output, including explicit limit and provider failure results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.fork_types import (
    ForkInputExhausted,
    ProviderForkRejected,
    ProviderForkResult,
)
from core.fork_types import FORK_CONTEXT_TRUNCATED, partial_provider_error
from core.fork_runtime import ForkKind, ForkStepLimitReached
from infra.provider_usage import TokenUsage, fork_receipt, from_anthropic
from llm.error_cause import USAGE_LIMIT
from subagents.provider_context_limits import is_provider_context_limit

#: The model Claude Code names on an assistant record it wrote itself to carry an error.
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True, slots=True)
class ForkLimit:
    """A usage-limit refusal read from structured fork output, never from narrative text.

    ``refusals`` are the ``rate_limit_info`` of every refusing ``rate_limit_event``;
    ``text`` is an ``is_error`` result's text when that text is a usage limit.
    """

    refusals: tuple[dict[str, Any], ...]
    text: str | None

    @property
    def detail(self) -> str:
        if self.text:
            return self.text
        kinds = ", ".join(str(info.get("rateLimitType") or "unnamed") for info in self.refusals)
        return f"Claude Code refused the call at its usage limit ({kinds})"


def _records(stdout: bytes):
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            yield record


def fork_limit(stdout: bytes) -> ForkLimit | None:
    """The usage-limit refusal in a fork's whole stdout, read after every record.

    A refusing ``rate_limit_event`` may come before or after the result, so this
    is decided only once all of stdout is read.
    """
    from connections.claude_usage import refuses
    from llm.error_cause import harness_cause_kind

    refusals: list[dict[str, Any]] = []
    text = None
    for record in _records(stdout):
        if record.get("type") == "rate_limit_event" and refuses(record.get("rate_limit_info")):
            refusals.append(record["rate_limit_info"])
        elif record.get("type") == "result" and record.get("is_error") is True:
            terminal = record.get("result")
            if isinstance(terminal, str) and harness_cause_kind("claude-code", None, terminal) == "usage_limit":
                text = terminal
    return ForkLimit(tuple(refusals), text) if refusals or text else None


def _assistant_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, dict) and message.get("model") == SYNTHETIC_MODEL:
        return ""  # Claude Code's own error text, never the model's output.
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
    if record.get("is_error") is True:
        return ""  # An error result's text is never fork output.
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
    failed, terminal_seen = False, False
    calls: dict[str, TokenUsage] = {}
    for record in _records(stdout):
        part = _result_body(record, has_body=bool(bodies))
        if part.strip():
            bodies.append(part)
        if record.get("type") == "assistant":
            message = record.get("message") or {}
            measured = from_anthropic(message.get("usage")) if isinstance(message, dict) else None
            if measured is not None:
                calls[str(message.get("id") or f"unidentified-{len(calls)}")] = measured
        if record.get("type") == "result":
            terminal_seen = True
            if record.get("subtype") == "error_max_turns":
                raise ForkStepLimitReached(f"{kind.name} reached its Claude Code turn limit")
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else usage
            if record.get("is_error") is True:
                terminal = record.get("result")
                diagnostic = str(terminal) if terminal is not None else "result error"
                truncated = truncated or is_provider_context_limit(record)
                failed = True
    # Decided once every record is read: a refusal may follow the result.
    limit = fork_limit(stdout)
    if not truncated and (failed or (limit is not None and not terminal_seen)):
        kind.provider_failure(
            "claude-code", diagnostic or (limit.detail if limit else "result error"),
            USAGE_LIMIT if limit is not None else None,
        )
    if not (body := "\n".join(bodies)).strip():
        if truncated:
            raise ForkInputExhausted(f"{FORK_CONTEXT_TRUNCATED}: {diagnostic or 'no narrative'}")
        detail = "; ".join(filter(None, (_no_body_detail(stdout), process)))
        raise ProviderForkRejected(f"Claude Code {kind.name} produced no body: {detail}")
    measured = from_anthropic(usage)
    if measured is None and calls:
        measured = sum(calls.values(), TokenUsage())
    if truncated:
        diagnostic = FORK_CONTEXT_TRUNCATED
    elif failed and diagnostic:
        diagnostic = partial_provider_error(diagnostic)  # assistant output cut short by the error
        body = _whole_messages(body)
    return ProviderForkResult(body, fork_receipt(measured), diagnostic)


def _whole_messages(body: str) -> str:
    """End a body of finished assistant records on a line break: its last line is complete."""
    return body if body.endswith("\n") else body + "\n"


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
    if returncode not in {None, 0}:
        kind.provider_failure("claude-code", diagnostic)  # an exit text naming a limit keeps nothing
        combined = partial_provider_error(combined)  # the process failed after this output
        return ProviderForkResult(_whole_messages(parsed.raw_body), parsed.usage, combined)
    return ProviderForkResult(parsed.raw_body, parsed.usage, combined)
