"""Local validation of the payload fields consumed by GitHub outcome facts."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from infra.outcome_event_input import OutcomeInputRefusal

# Source-field shape only; datetime remains the parser for calendar/time validity.
_SOURCE_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class PayloadRefusal(OutcomeInputRefusal):
    """A consumed payload field cannot describe the requested raw fact."""


def mapping(value: Any) -> dict[str, Any]:
    """Require a JSON object before accessing its fields."""
    if not isinstance(value, dict):
        raise PayloadRefusal("expected an object")
    return value


def members(value: Any) -> list[Any]:
    """Require a JSON array before reading its members."""
    if not isinstance(value, list):
        raise PayloadRefusal("expected an array")
    return value


def required(value: dict[str, Any], key: str) -> Any:
    """Read a consumed required field, refusing an absent key explicitly."""
    if key not in value:
        raise PayloadRefusal("required field absent")
    return value[key]


def text(value: Any) -> str:
    """Require a nonempty string, without manufacturing an identity by coercion."""
    if not isinstance(value, str) or not value.strip():
        raise PayloadRefusal("expected a nonempty string")
    return value


def number(value: Any) -> int:
    """Require a positive integer identity; JSON booleans are not integers here."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PayloadRefusal("expected a positive integer")
    return value


def reference_number(value: str) -> int:
    """Validate a decimal PR/issue identity extracted from untrusted body text."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PayloadRefusal("invalid reference identity") from exc
    return number(parsed)


def boolean(value: Any) -> bool:
    """Require the JSON boolean used to describe a PR state."""
    if not isinstance(value, bool):
        raise PayloadRefusal("expected a boolean")
    return value


def timestamp(value: Any) -> str:
    """Require GitHub's full zoned date-time; never infer midnight or a timezone."""
    value = text(value)
    if _SOURCE_DATE_TIME.fullmatch(value) is None:
        raise PayloadRefusal("expected a zoned date-time")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadRefusal("expected a valid date-time") from exc
    return value


def pr_numbers(value: Any) -> list[int]:
    """Read the identities of the PR members a run or suite declares."""
    return [number(required(mapping(member), "number")) for member in members(value)]


def body(value: Any) -> str:
    """Accept a nullable PR body, refusing non-text rather than parsing its repr."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PayloadRefusal("expected text or null body")
    return value
