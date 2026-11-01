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


def _one_per_key(rows: Sequence[Event], keys: Sequence[str], *, newest: bool) -> list[Event]:
    """One row per key value, by source time."""
    selected: dict[tuple[Any, ...], Event] = {}
    for row in sorted(rows, key=lambda item: item.ts, reverse=newest):
        selected.setdefault(tuple(attribute(row, key) for key in keys), row)
    return list(selected.values())


def select(operand: Mapping[str, Any], facts: Sequence[Event]) -> list[Event]:
    """Select and optionally deduplicate an operand before applying joins.

    ``latest`` reads each item's current row, so ``where`` applies after it: a relabelled item leaves its
    old value. ``first`` reads the earliest row that qualifies, so ``where`` applies before it.
    """
    names = operand.get("event", [])
    names = [names] if isinstance(names, str) else names
    rows = [row for row in facts if row.name in names]
    where = operand.get("where", {})
    if "latest" in operand:
        rows = [row for row in _one_per_key(rows, operand["latest"], newest=True) if matches_where(row, where)]
    else:
        rows = [row for row in rows if matches_where(row, where)]
        if "first" in operand:
            rows = _one_per_key(rows, operand["first"], newest=False)
    clauses = operand.get("match", [])
    indexes = []
    for clause in clauses:
        index: dict[Any, list[Event]] = {}
        for fact in facts:
            if fact.name == clause["event"] and matches_where(fact, clause.get("where", {})):
                index.setdefault(attribute(fact, clause["on"]), []).append(fact)
        indexes.append(index)
    return [matched for row in rows if (matched := _join(row, clauses, indexes)) is not None]


def _join(row: Event, clauses: Sequence[Mapping[str, Any]],
          indexes: Sequence[Mapping[Any, list[Event]]]) -> Event | None:
    for clause, index in zip(clauses, indexes):
        value = attribute(row, clause["on"])
        matches = index.get(value, []) if value is not None else []
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
