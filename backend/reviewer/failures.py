"""Failure taxonomy for the reviewer backends.

Owns the exception classes and error-text classification shared by the
Claude Code and Codex reviewer backends, plus Claude Code's CLI-failure
helpers and ``--max-turns`` budget resolution. Names are re-exported from
``reviewer_backends`` so importers are unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, NoReturn

from subagents.provider_usage_limits import (
    is_provider_usage_or_quota_limit as is_usage_or_quota_limit,
)

log = logging.getLogger(__name__)


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


# The turn budget must not be the binding constraint. A real PR review at
# effort=high reads the diff, then the cited files and the standards sections,
# then composes findings; that alone is dozens of tool turns on a large diff.
# Measured on PR #1780 (4,298 additions across 69 files) on 2026-09-16: the
# review exhausted a budget of 30 in ~355 s (~11.5 s/turn) and stopped with
# stop_reason=tool_use, i.e. mid-tool, still working. The Codex reviewer
# (invoke_codex_reviewer) passes no turn cap at all — it is bounded only by the
# shared 1800 s subprocess timeout — so a low Claude cap is an artificial
# Claude-only failure that Codex cannot have.
#
# 200 puts the cap above what the shared timeout can reach (~1800 s / ~11.5 s
# per turn ≈ 157 turns), so wall clock decides when a review stops, for both
# backends alike. It stays finite so a pathological fast loop is still bounded.
# Raise it further only together with the 1800 s subprocess timeout that both
# backends pass to subprocess.run in reviewer_backends.py.
DEFAULT_CC_MAX_TURNS = 200


def cc_max_turns() -> str:
    """Claude reviewer turn budget; CORSIA_REVIEWER_MAX_TURNS overrides."""
    raw = os.environ.get("CORSIA_REVIEWER_MAX_TURNS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return str(value)
        log.warning("Ignoring invalid CORSIA_REVIEWER_MAX_TURNS=%r", raw)
    return str(DEFAULT_CC_MAX_TURNS)


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
            and (usage is None or isinstance(usage, dict) and usage.get("input_tokens", 0) == 0)
        ):
            return CCUnavailableError(detail)
    return RuntimeError(detail)


def raise_cli_failure(returncode: int, output: str, stderr: str) -> NoReturn:
    """Classify and raise a non-zero ``claude -p`` exit.

    The result and envelope signals precede the truncated raw output so the
    CLI's explanation survives truncation.
    """
    envelope = parse_envelope(output)
    signals = envelope_signals(envelope) if envelope else ""
    result = str(envelope.get("result") or "")[:200] if envelope else ""
    prefix = " | ".join(part for part in (result, signals) if part)
    prefix = f"{prefix} | " if prefix else ""
    subtype = envelope.get("subtype") if envelope else None
    raise classify_cc_error(
        f"{output} {stderr} subtype={subtype}",
        f"claude -p failed (rc={returncode}): {prefix}{(output if envelope else stderr or output)[:200]}",
        envelope,
    )
