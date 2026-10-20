"""Google-specific calendar-list formatting."""

from __future__ import annotations

from typing import Any


def _event_time(event: dict, key: str) -> str | None:
    value = event.get(key)
    if not isinstance(value, dict):
        return None
    candidate = value.get("dateTime", value.get("date"))
    return candidate if isinstance(candidate, str) else None


def event_summaries(items: Any) -> list[dict[str, Any]]:
    """The fields calendar_list returns for each Google event; anything malformed is skipped."""
    events = []
    for event in items if isinstance(items, list) else []:
        if not isinstance(event, dict):
            continue
        attendees = event.get("attendees")
        events.append({
            "id": event.get("id"), "start": _event_time(event, "start"),
            "end": _event_time(event, "end"), "title": event.get("summary", ""),
            "attendees": [item["email"] for item in attendees if isinstance(item, dict) and isinstance(item.get("email"), str)] if isinstance(attendees, list) else [],
            "free/busy": "free" if event.get("transparency") == "transparent" else "busy",
        })
    return events
