"""Strict payload checks shared by the measurement report validators.

Each check raises the caller's own malformed-report error, so a producer still
sees which report type it got wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol, Self, TypeVar


class _Report(Protocol):
    @classmethod
    def parse(cls, raw: Any) -> Self: ...

    def as_contract(self) -> dict[str, Any]: ...


R = TypeVar("R", bound=_Report)


def revalidate(report: R | Mapping[str, Any], report_type: type[R]) -> R:
    """Parse a raw report, or re-validate a typed one built by hand."""
    raw = report.as_contract() if isinstance(report, report_type) else report
    return report_type.parse(raw)


def exact_keys(
    raw: Any, required: set[str], what: str, error: type[ValueError],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise error(f"{what} must be an object")
    missing, extra = required - set(raw), set(raw) - required
    if missing or extra:
        raise error(f"{what}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return raw


def text(value: Any, what: str, error: type[ValueError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{what} must be a non-empty string")
    return value


def report_date(value: Any, error: type[ValueError]) -> str:
    """A canonical ``YYYY-MM-DD``; ``date.fromisoformat`` alone also takes ``20261023``."""
    day = text(value, "report_date", error)
    try:
        canonical = date.fromisoformat(day).isoformat() == day
    except ValueError:
        canonical = False
    if not canonical:
        raise error(f"report_date {day!r} is not YYYY-MM-DD")
    return day
