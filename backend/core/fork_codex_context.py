"""Fork-local Codex occupancy: a last-call receipt plus unsent growth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.fork_types import FORK_TOOL_ROUND_TRIP_TOKENS
from core.token_estimation import measure_request
from subagents.codex_durable_receipts import normalize_receipt, usage_identity


def _last_total(last: object, *, camel: bool = False) -> int | None:
    if not isinstance(last, dict):
        return None
    fields = ("inputTokens", "outputTokens") if camel else ("input_tokens", "output_tokens")
    total = 0
    for field in fields:
        value = last.get(field)
        if type(value) is not int or value < 0:
            return None
        total += value
    return total


def frozen_context_tokens(
    path: Path, prefix_bytes: int, *, target, fallback_tokens: int,
) -> int:
    """Read only the frozen prefix; lifetime usage never becomes occupancy.

    Model framing remains an estimate. A provider receipt replaces the earlier
    estimate, but cannot include tool outputs recorded after that model call.
    """
    tokens, tool_tokens = fallback_tokens, 0
    receipt_identity = None
    with path.open("rb") as stream:
        remaining = prefix_bytes
        while remaining:
            line = stream.readline(remaining)
            if not line:
                raise ValueError("fork rollout ended before its frozen boundary")
            remaining -= len(line)
            record = json.loads(line)
            payload = record.get("payload") or {}
            if record.get("type") == "response_item":
                count = measure_request(payload, target=target).tokens
                tokens += count
                if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                    tool_tokens += count
            elif record.get("type") == "token_usage_record":
                usage = normalize_receipt(payload)
                identity = usage_identity(usage)
                if identity != receipt_identity:
                    tokens = usage["last"]["inputTokens"] + usage["last"]["outputTokens"]
                    tool_tokens = 0
                    receipt_identity = identity
            elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                last = _last_total(info.get("last_token_usage"))
                identity = usage_identity(normalize_receipt({
                    "usage": info.get("last_token_usage"),
                    "thread_token_usage": info.get("total_token_usage"),
                })) if last is not None else None
                if last is not None and identity != receipt_identity:
                    tokens, tool_tokens = last + tool_tokens, 0
                    receipt_identity = identity
    return tokens


class CodexForkContext:
    """Count one fork's complete model receipt plus its aggregate tool replies."""

    def __init__(self, target, input_tokens: int | None) -> None:
        self.target = target
        self.base = input_tokens or 0
        self.input_seeded = input_tokens is not None
        self.results = 0
        self.receipt_identity: object = None
        self.pending_inputs: dict[str, int] = {}
        self.consumed_inputs: set[str] = set()

    @property
    def tokens(self) -> int:
        return self.base + self.results + sum(self.pending_inputs.values())

    @property
    def fits(self) -> bool:
        return self.tokens <= self.target.context_window

    def leaves_output_room(self, max_output_tokens: int) -> bool:
        """True when the input still leaves room for the fork's own output."""
        reserve = max_output_tokens + FORK_TOOL_ROUND_TRIP_TOKENS
        return self.tokens + reserve <= self.target.context_window

    def record_receipt(self, usage: dict[str, Any]) -> None:
        """Use live input/output, including hidden reasoning, before returning tools."""
        last = _last_total(usage.get("last"), camel=True)
        identity = usage_identity(usage)
        if last is None or _last_total(usage.get("total"), camel=True) is None:
            raise ValueError("Codex fork usage receipt lacks input/output counts")
        if identity != self.receipt_identity:
            self.base, self.results = last, 0
            self.receipt_identity = identity
            for input_id in self.consumed_inputs:
                self.pending_inputs.pop(input_id, None)
            self.consumed_inputs.clear()

    def accept_tool_result(self, reply: dict[str, Any]) -> bool:
        """Count the actual wire reply, including media, before the pipe write."""
        parts = [
            {"type": "image_url"} if item.get("type") == "inputImage"
            else {"type": "text", "text": item.get("text", "")}
            for item in reply.get("contentItems", ())
        ]
        self.results += measure_request({
            "messages": [{"role": "tool", "content": parts}],
        }, target=self.target).tokens
        return self.fits
