"""Safe terminal classifications and usage-number normalization for isolated inference providers."""

from __future__ import annotations

import math
import re


def nonnegative_count(value: object) -> int | None:
    """Normalize an integral non-negative provider count or discard it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return None
    value = int(value)
    return value if value >= 0 else None


def nonnegative_cost(value: object) -> float | None:
    """Normalize a finite non-negative provider cost or discard it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


def provider_error_class(content: object) -> str:
    """Return only bounded status/type/code tokens from an untrusted error."""
    detail = str(content or "")
    status = re.search(r"\b([45]\d\d)\b", detail)
    code = re.search(
        r"(?:^|[,{\s])[\"']?(?:type|code|error)[\"']?\s*[ :=]+[\"']?([a-z][a-z0-9_]{0,47})(?=$|[\s,}\"'])",
        detail,
    )
    parts = ["provider_error"]
    if status:
        parts.append(f"http_{status.group(1)}")
    if code:
        parts.append(code.group(1).lower())
    return ":".join(parts)[:120]
