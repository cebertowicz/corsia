"""iCalendar text from a host into the fields ``calendar_list`` returns for each event.

The request asks the host to expand recurring events. A component that still carries an
``RRULE`` came back unexpanded, so its occurrences in the window are expanded here with
``dateutil``. Anything malformed is skipped, never raised: one odd event must not hide the rest.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from dateutil.rrule import rrulestr
from icalendar import Calendar

MAX_EVENTS = 250
_OCCURRENCE_CAP = 500


def _as_utc(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _iso(value: date | datetime) -> str:
    return value.isoformat()


def _attendees(component: Any) -> list[str]:
    raw = component.get("ATTENDEE")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    return [str(item)[7:] for item in items if str(item).lower().startswith("mailto:")]


def _end_of(component: Any, start: date | datetime) -> date | datetime:
    if component.get("DTEND") is not None:
        return component.decoded("DTEND")
    if component.get("DURATION") is not None:
        return start + component.decoded("DURATION")
    return start + timedelta(days=1) if not isinstance(start, datetime) else start


def _summary(component: Any, start: date | datetime, end: date | datetime, *, instance: bool) -> dict[str, Any]:
    uid = str(component.get("UID", ""))
    return {
        "id": f"{uid}@{_iso(start)}" if instance or component.get("RECURRENCE-ID") is not None else uid,
        "start": _iso(start), "end": _iso(end), "title": str(component.get("SUMMARY", "")),
        "attendees": _attendees(component),
        "free/busy": "free" if str(component.get("TRANSP", "")).upper() == "TRANSPARENT" else "busy",
    }


def _occurrences(component: Any, start: date | datetime, window: tuple[datetime, datetime]) -> list[datetime]:
    """Every start of a rule-repeated event inside the window, minus its EXDATEs."""
    lower, upper = window
    anchor = start if isinstance(start, datetime) else datetime(start.year, start.month, start.day)
    rule = rrulestr(component["RRULE"].to_ical().decode(), dtstart=anchor)
    if anchor.tzinfo is None:
        lower, upper = lower.replace(tzinfo=None), upper.replace(tzinfo=None)
    excluded = set()
    for group in component.get("EXDATE") or []:
        for item in group.dts if hasattr(group, "dts") else [group]:
            excluded.add(_as_utc(item.dt).replace(microsecond=0))
    found = []
    for occurrence in rule.between(lower, upper, inc=True):
        if _as_utc(occurrence).replace(microsecond=0) not in excluded:
            found.append(occurrence)
        if len(found) >= _OCCURRENCE_CAP:
            break
    return found


def _expanded(component: Any, start: date | datetime, end: date | datetime, window: tuple[datetime, datetime]) -> list[dict]:
    duration = _as_utc(end) - _as_utc(start)
    found = []
    for occurrence in _occurrences(component, start, window):
        shown = occurrence if isinstance(start, datetime) else occurrence.date()
        found.append(_summary(component, shown, shown + duration, instance=True))
    return found


def summaries(texts: list[str], window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
    """Every event in the host's calendars, in start order, cut at ``MAX_EVENTS``."""
    events: list[tuple[datetime, dict[str, Any]]] = []
    for text in texts:
        try:
            calendar = Calendar.from_ical(text)
        except ValueError:
            continue
        for component in calendar.walk("VEVENT"):
            try:
                start = component.decoded("DTSTART")
                end = _end_of(component, start)
                found = (_expanded(component, start, end, window) if component.get("RRULE") is not None
                         else [_summary(component, start, end, instance=False)])
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            events.extend((_as_utc(datetime.fromisoformat(item["start"]) if "T" in item["start"]
                                   else date.fromisoformat(item["start"])), item) for item in found)
    events.sort(key=lambda pair: pair[0])
    return [item for _, item in events[:MAX_EVENTS]]
