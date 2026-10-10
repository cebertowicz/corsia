"""Independent raw-response usage accounting for Harbor validation."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UsageReceipt:
    """One provider response recorded independently of CORSIA's ledger."""

    unit_id: str
    call_index: int
    timestamp: float
    elapsed_seconds: float
    usage: dict[str, Any] | None
    model: str | None = None
    path: str | None = None
    status: int | None = None

    def as_json(self) -> str:
        """Serialize one stable JSONL receipt row; ``usage`` is the raw provider object."""
        return json.dumps({
            "unit_id": self.unit_id,
            "call_index": self.call_index,
            "timestamp": self.timestamp,
            "elapsed_seconds": self.elapsed_seconds,
            "usage": self.usage,
            "model": self.model,
            "path": self.path,
            "status": self.status,
        }, sort_keys=True)


class ProviderCallCapExceeded(RuntimeError):
    """Raised before a unit would exceed its independently metered call cap."""


class JsonlMeter:
    """Append raw usage receipts and enforce one explicit unit call cap."""

    def __init__(self, receipt_path: Path, *, unit_id: str, call_cap: int) -> None:
        if call_cap < 1:
            raise ValueError("call_cap must be positive")
        self._path = receipt_path
        self._unit_id = unit_id
        self._cap = call_cap
        self._count = 0
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def call_count(self) -> int:
        """Return the number of charged provider calls."""
        return self._count

    @property
    def receipt_path(self) -> Path:
        """Return the JSONL receipt file this meter appends to."""
        return self._path

    def reserve_call(self) -> int:
        """Charge the next call before it reaches the provider."""
        with self._lock:
            if self._count >= self._cap:
                raise ProviderCallCapExceeded(
                    f"provider call cap {self._cap} reached for {self._unit_id}"
                )
            self._count += 1
            return self._count

    def record_response(
        self,
        call_index: int,
        response: dict[str, Any],
        started_at: float,
        *,
        path: str | None = None,
        status: int | None = None,
    ) -> UsageReceipt:
        """Append the raw response usage without consulting CORSIA usage records.

        A charged call whose response carries no usage is still recorded, with
        ``usage: null``, so it stays visible against the cap.
        """
        raw_usage = response.get("usage")
        model = response.get("model")
        receipt = UsageReceipt(
            self._unit_id,
            call_index,
            time.time(),
            time.monotonic() - started_at,
            raw_usage if isinstance(raw_usage, dict) else None,
            model if isinstance(model, str) else None,
            path,
            status,
        )
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(receipt.as_json() + "\n")
        return receipt


def normalized_tokens(usage: dict[str, Any] | None) -> dict[str, int]:
    """Map a raw chat-completions or responses usage object to Harbor's fields.

    ``input`` includes cached tokens, matching Harbor's ``n_input_tokens``.
    """
    if not usage:
        return {"input": 0, "cache": 0, "output": 0}
    # Chat completions name the fields prompt/completion; the responses API input/output.
    chat = "prompt_tokens" in usage or "completion_tokens" in usage
    input_key, output_key, details_key = (
        ("prompt_tokens", "completion_tokens", "prompt_tokens_details") if chat
        else ("input_tokens", "output_tokens", "input_tokens_details")
    )
    details = usage.get(details_key)
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return {
        "input": int(usage.get(input_key) or 0),
        "cache": int(cached or 0),
        "output": int(usage.get(output_key) or 0),
    }


def receipt_rows(receipt_path: Path, unit_id: str | None = None) -> list[dict[str, Any]]:
    """Read receipt rows, optionally for one unit."""
    if not receipt_path.exists():
        return []
    rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines() if line]
    return [row for row in rows if unit_id is None or row["unit_id"] == unit_id]


def harbor_totals(receipt_path: Path, unit_id: str | None = None) -> dict[str, int]:
    """Sum one unit's raw receipts into the token totals exported to Harbor."""
    totals = {"input": 0, "cache": 0, "output": 0, "calls": 0, "calls_without_usage": 0}
    for row in receipt_rows(receipt_path, unit_id):
        totals["calls"] += 1
        if row.get("usage") is None:
            totals["calls_without_usage"] += 1
        for key, value in normalized_tokens(row.get("usage")).items():
            totals[key] += value
    return totals


def export_meter_totals(context: Any, receipt: Path, unit_id: str) -> dict[str, Any]:
    """Fill a Harbor ``AgentContext``'s token fields from the raw meter receipt alone."""
    totals = harbor_totals(receipt, unit_id)
    context.n_input_tokens = totals["input"]
    context.n_cache_tokens = totals["cache"]
    context.n_output_tokens = totals["output"]
    summary: dict[str, Any] = {"meter_receipt": str(receipt), "meter_totals": totals}
    if totals["calls"]:
        summary["median_seconds_per_call"] = median_seconds_per_call(receipt, unit_id)
    context.metadata = {**(context.metadata or {}), **summary}
    return summary


def median_seconds_per_call(receipt_path: Path, unit_id: str | None = None) -> float:
    """Return the median observed elapsed seconds for a successful probe's calls."""
    values = sorted(float(row["elapsed_seconds"]) for row in receipt_rows(receipt_path, unit_id))
    if not values:
        raise ValueError("receipt has no provider calls")
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def measured_call_cap(seconds_per_call: float) -> int:
    """Implement contract v2's frozen-at-preflight per-unit cap formula."""
    if seconds_per_call <= 0:
        raise ValueError("seconds_per_call must be positive")
    return max(300, math.ceil(1.5 * 1200 / seconds_per_call))
