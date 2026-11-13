"""Failure taxonomy for the Claude Code CLI helpers.

Owns the exception classes and error-text classification for a failed
``claude -p`` run; host-access reviewer code imports ``CCUnavailableError``.
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

from subagents.provider_usage_limits import (
    is_provider_usage_or_quota_limit as is_usage_or_quota_limit,
)


class CCRateLimitError(Exception):
    """Raised when the reviewer backend reports a retryable rate limit."""


class CCUnavailableError(Exception):
    """Raised when the configured cloud reviewer cannot serve the request."""


class CCMaxTurnsError(RuntimeError):
    """Raised when Claude Code exhausted its ``--max-turns`` turn budget.

    Subclasses ``RuntimeError`` so every existing generic failure site keeps
    its behavior; the dedicated type exists so the outcome is distinguishable
    in logs and gate status instead of a bare ``rc=1`` blob.
    """


def envelope_signals(envelope: dict[str, Any]) -> str:
    """Compact signal summary from a CLI result envelope, for failure detail.

    Failure text truncates stdout to 200 chars, which routinely cuts off the
    envelope's ``subtype``; the signals ride in front of the truncation so
    logs and gate status always name the cause class.
    """
    pairs = []
    for key in ("subtype", "stop_reason", "num_turns", "total_cost_usd"):
        value = envelope.get(key)
        if value is not None:
            pairs.append(f"{key}={value}")
    return " ".join(pairs)


def envelope_failure_detail(envelope: dict[str, Any], output: str, stderr: str) -> str:
    """Failure detail for a CLI error envelope, whatever the exit code.

    The CLI's result text and the signals precede the truncated raw output so
    the explanation survives truncation; stderr follows when there is any.
    """
    parts = (
        str(envelope.get("result") or "")[:200],
        envelope_signals(envelope),
        output[:200],
        f"stderr: {stderr[:200]}" if stderr else "",
    )
    return " | ".join(part for part in parts if part)


def parse_envelope(output: str) -> dict[str, Any] | None:
    """Parse a CLI stdout envelope, or None when it is not JSON."""
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Re-exported above for existing reviewer imports; the marker set has one owner.


def classify_cc_error(
    error_text: str, detail: str, envelope: dict[str, Any] | None = None,
) -> Exception:
    """Map raw CC error text to the right exception type."""
    low = str(error_text).lower()
    if "error_max_turns" in low:
        return CCMaxTurnsError(detail)
    if is_usage_or_quota_limit(low):
        return CCUnavailableError(detail)
    if "rate_limit" in low or "rate limit" in low or "429" in low:
        return CCRateLimitError(detail)
    if (
        "temporarily unavailable" in low
        or "overloaded" in low
        or "529" in low
        or "model not found" in low
        or "model_not_found" in low
        or "model not available" in low
        or "unknown model" in low
        or "invalid model" in low
        or "issue with the selected model" in low
        or "no endpoints" in low
        or "decommissioned" in low
        or "deprecated" in low
        or "delisted" in low
    ):
        return CCUnavailableError(detail)
    if envelope and envelope.get("is_error"):
        usage = envelope.get("usage")
        if (
            envelope.get("duration_api_ms") == 0
            and envelope.get("total_cost_usd") == 0
            and (usage is None or (isinstance(usage, dict) and not usage.get("input_tokens")))
        ):
            return CCUnavailableError(detail)
    return RuntimeError(detail)


def raise_cli_failure(returncode: int, output: str, stderr: str) -> NoReturn:
    """Classify and raise a non-zero ``claude -p`` exit."""
    envelope = parse_envelope(output)
    subtype = envelope.get("subtype") if envelope else None
    detail = (
        envelope_failure_detail(envelope, output, stderr) if envelope
        else (stderr or output)[:200]
    )
    raise classify_cc_error(
        f"{output} {stderr} subtype={subtype}",
        f"claude -p failed (rc={returncode}): {detail}",
        envelope,
    )
