"""Selection and joins for declared KPI event operands."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Event:
    """One stored fact, with its source time, attributes and evidence."""
    name: str
    ts: datetime
    attrs: Mapping[str, Any]
    subject: str | None
    version: str | None
    label: str


def timestamp(value: str) -> datetime:
    """Parse a source timestamp into an ordered UTC instant."""
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def attribute(row: Event, key: str) -> Any:
    """Return a matchable attribute, including the reserved subject."""
    return row.subject if key == "subject" else row.attrs.get(key)


def matches_where(row: Event, where: Mapping[str, Any]) -> bool:
    """Apply declared equality and id presence predicates."""
    for key, expected in where.items():
        actual = attribute(row, key)
        if expected == "present":
            if actual is None or actual == "":
                return False
        elif expected == "absent":
            if actual is not None and actual != "":
                return False
        elif isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def select(operand: Mapping[str, Any], facts: Sequence[Event]) -> list[Event]:
    """Select and optionally deduplicate an operand before applying joins."""
    names = operand.get("event", [])
    names = [names] if isinstance(names, str) else names
    rows = [row for row in facts if row.name in names and matches_where(row, operand.get("where", {}))]
    order = "latest" if "latest" in operand else "first" if "first" in operand else None
    if order:
        selected: dict[tuple[Any, ...], Event] = {}
        for row in sorted(rows, key=lambda item: item.ts, reverse=order == "latest"):
            selected.setdefault(tuple(attribute(row, key) for key in operand[order]), row)
        rows = list(selected.values())
    return [matched for row in rows if (matched := _join(row, operand.get("match", []), facts)) is not None]


def _join(row: Event, clauses: Sequence[Mapping[str, Any]], facts: Sequence[Event]) -> Event | None:
    for clause in clauses:
        key = clause["on"]
        value = attribute(row, key)
        matches = [fact for fact in facts if value is not None and fact.name == clause["event"]
                   and attribute(fact, key) == value and matches_where(fact, clause.get("where", {}))]
        count = len(matches)
        mode = clause["mode"]
        if (mode == "exists" and not count) or (mode == "absent" and count) or (mode == "exactly_one" and count != 1):
            return None
        if clause.get("anchor") and matches:
            anchor = max(matches, key=lambda fact: fact.ts)
            row = replace(row, ts=anchor.ts, version=anchor.version)
    return row


def distinct(rows: Sequence[Event], key: str | None) -> list[Event]:
    """Keep one row per distinct attribute, or per source day."""
    if not key:
        return list(rows)
    chosen: dict[Any, Event] = {}
    for row in rows:
        value = row.ts.date() if key == "day" else attribute(row, key)
        chosen.setdefault(value, row)
    return list(chosen.values())
