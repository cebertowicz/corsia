"""The one pass over a script's stdout: readings, measure lines and what was wrong.

Each line is JSON and is dispatched on its single top-level key: ``{"series", "value"}``
is a reading, ``{"measure": {...}}`` is a measure line. A reading problem is a run
error. A measure line that is malformed, or past the per-run cap, is refused and
counted instead: it never fails the run or wakes the owner (design section 4.4.5).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from automation import script_schedules as store
from infra.measure_admission import UNNAMED_LINE

MAX_MEASURE_LINES = 200


@dataclass(frozen=True)
class Reading:
    series: str
    value: float


@dataclass(frozen=True)
class LineRefusal:
    """A measure line refused before admission; ``event_name`` is the counter's label."""

    event_name: str
    text: str


@dataclass
class ParsedOutput:
    readings: list[Reading] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)
    refused: list[LineRefusal] = field(default_factory=list)
    over_cap: int = 0
    problems: list[str] = field(default_factory=list)


def parse_output(stdout: bytes, registered: dict[str, store.Series]) -> ParsedOutput:
    """Read stdout once. Output that is not UTF-8 admits nothing."""
    parsed = ParsedOutput()
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        parsed.problems.append("output is not valid UTF-8")
        return parsed
    readings: dict[str, Reading] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            parsed.problems.append(f"line {number}: not a JSON object")
            continue
        if isinstance(item, dict) and set(item) == {"measure"}:
            _take_measure(parsed, number, item["measure"])
            continue
        if isinstance(item, dict) and set(item) == {"event"}:
            problem = _take_event(parsed, item["event"])
            if problem:
                parsed.problems.append(f"line {number}: {problem}")
            continue
        problem = _read_line(item, registered, readings)
        if problem:
            parsed.problems.append(f"line {number}: {problem}")
    parsed.readings = list(readings.values())
    return parsed


def _take_measure(parsed: ParsedOutput, number: int, measure: Any) -> None:
    if len(parsed.measures) + len(parsed.refused) >= MAX_MEASURE_LINES:
        parsed.over_cap += 1
        return
    problem = _measure_problem(measure)
    if problem is None:
        parsed.measures.append(measure)
        return
    name = measure.get("event.name") if isinstance(measure, dict) else None
    parsed.refused.append(LineRefusal(
        name if isinstance(name, str) and name else UNNAMED_LINE,
        f"measure line {number}: {problem}; the line was dropped.",
    ))


def _measure_problem(measure: Any) -> str | None:
    if not isinstance(measure, dict):
        return '"measure" must be an object with "event.name" and "attributes"'
    if not isinstance(measure.get("event.name"), str) or not measure["event.name"]:
        return '"event.name" must be a non-empty string naming a declared event'
    if not isinstance(measure.get("attributes", {}), dict):
        return '"attributes" must be an object of attribute names and values'
    if "time" in measure and not isinstance(measure["time"], str):
        return '"time" must be an ISO 8601 string, or left out'
    return None


def _take_event(parsed: ParsedOutput, event: Any) -> str | None:
    if not isinstance(event, dict) or set(event) != {"type", "facts"}:
        return '"event" must be an object with exactly "type" and "facts"'
    if not isinstance(event["type"], str) or not event["type"]:
        return 'event type must be a non-empty declared event name'
    if not isinstance(event["facts"], dict):
        return 'event facts must be an object'
    parsed.events.append(event)
    return None


def _read_line(
    item: Any, registered: dict[str, store.Series], readings: dict[str, Reading],
) -> str | None:
    if not isinstance(item, dict) or set(item) != {"series", "value"}:
        return 'expected exactly {"series": ..., "value": ...}, {"event": ...}, or {"measure": ...}'
    key, value = item["series"], item["value"]
    if not isinstance(key, str) or key not in registered:
        return "series is not registered on this schedule"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{key} value is not a number"
    if not math.isfinite(value):
        return f"{key} value is not finite"
    if key in readings:
        return f"{key} appears more than once"
    readings[key] = Reading(key, float(value))
    return None
