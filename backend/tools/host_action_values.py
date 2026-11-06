"""Value validation for reviewed host actions."""

import math
from typing import Any, Mapping

from tools.host_access_errors import HostAccessBlocked


def text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    return value


def boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HostAccessBlocked("Host action parameters are invalid.")
    return value


def integer(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostAccessBlocked("Host action parameters are invalid.")
    if value < minimum:
        raise HostAccessBlocked("Host action parameters are invalid.")
    return min(value, maximum)


def number(value: Any, field_name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    return normalized


def search_payload(canonical: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the search parameters bound into a reviewed action."""
    payload = {
        "pattern": text(params.get("pattern"), "pattern"),
        "all_threads": boolean(params.get("all_threads")),
    }
    if canonical == "grep":
        payload.update({
            "glob": params.get("glob"),
            "output_mode": params.get("output_mode") or "files",
            "case_insensitive": boolean(params.get("case_insensitive")),
            "context": integer(params.get("context"), 0, minimum=0, maximum=10),
            "max_results": integer(params.get("max_results"), 50, minimum=1, maximum=200),
        })
    return payload
