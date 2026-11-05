"""Value validation for reviewed host actions."""

import math
from typing import Any

from tools.host_access_errors import HostAccessBlocked


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    return value


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HostAccessBlocked("Host action parameters are invalid.")
    return value


def _integer(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostAccessBlocked("Host action parameters are invalid.")
    if value < minimum:
        raise HostAccessBlocked("Host action parameters are invalid.")
    return min(value, maximum)


def _number(value: Any, field_name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    return normalized
